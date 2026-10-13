# Copyright (c) 2026, Python 3.15 campaign contributors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
# ruff: noqa: T201

"""Disposable mmap/Windows residency probe; never an upstream fixture fix."""

import ctypes
import hashlib
import json
import mmap
import os
import platform
import sys
import sysconfig
import tempfile
import textwrap
import time
from collections import Counter
from pathlib import Path

SIZE = 32 * 1024 * 1024
STEP = (
    4096  # Match the original test, rather than silently changing its stride.
)


class WorkingSetEx(ctypes.Structure):
    # PSAPI_WORKING_SET_EX_INFORMATION: PVOID, ULONG_PTR union Flags.
    _fields_ = [("address", ctypes.c_void_p), ("flags", ctypes.c_size_t)]


class MemoryBasicInformation64(ctypes.Structure):
    # Explicit 64-bit form documented in winnt.h. Padding matches SDK offsets.
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64),
        ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", ctypes.c_uint32),
        ("alignment1", ctypes.c_uint32),
        ("RegionSize", ctypes.c_uint64),
        ("State", ctypes.c_uint32),
        ("Protect", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("alignment2", ctypes.c_uint32),
    ]


class WindowsAPI:
    def __init__(self):
        assert sys.platform == "win32"
        assert ctypes.sizeof(ctypes.c_void_p) == 8
        assert ctypes.sizeof(WorkingSetEx) == 16
        assert WorkingSetEx.flags.offset == 8
        assert ctypes.sizeof(MemoryBasicInformation64) == 48
        assert MemoryBasicInformation64.RegionSize.offset == 24
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        self.kernel.OpenProcess.restype = ctypes.c_void_p
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int
        self.psapi.QueryWorkingSetEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self.psapi.QueryWorkingSetEx.restype = ctypes.c_int
        self.kernel.VirtualQueryEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(MemoryBasicInformation64),
            ctypes.c_size_t,
        ]
        self.kernel.VirtualQueryEx.restype = ctypes.c_size_t
        self.kernel.GetProcessWorkingSetSizeEx.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.kernel.GetProcessWorkingSetSizeEx.restype = ctypes.c_int

    def snapshot(self, pid, base):
        import psutil

        handle = self.kernel.OpenProcess(
            0x0400, False, pid
        )  # QUERY_INFORMATION
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # Ordered observations, not an atomic snapshot; preserve timing.
            observed = {
                "pid": pid,
                "base": base,
                "start_ns": time.perf_counter_ns(),
            }
            process = psutil.Process(pid)
            observed["psutil_memory_info"] = process.memory_info()._asdict()
            observed["psutil_memory_footprint"] = (
                process.memory_footprint()._asdict()
            )
            low, high = ctypes.c_size_t(), ctypes.c_size_t()
            quota_flags = ctypes.c_uint32()
            if not self.kernel.GetProcessWorkingSetSizeEx(
                handle,
                ctypes.byref(low),
                ctypes.byref(high),
                ctypes.byref(quota_flags),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            observed["working_set_limits"] = {
                "minimum": low.value,
                "maximum": high.value,
                "raw_quota_flags": quota_flags.value,
            }
            pages = (WorkingSetEx * (SIZE // STEP))()
            for index, page in enumerate(pages):
                page.address = base + index * STEP
            if not self.psapi.QueryWorkingSetEx(
                handle, pages, ctypes.sizeof(pages)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            flags = [page.flags for page in pages]
            # ShareCount is meaningful only when Valid=1. Shared is bit 15 in
            # both valid and invalid SDK views. Retain all raw flags for audit.
            valid = [flag for flag in flags if flag & 1]
            observed["page_flags"] = flags
            observed["mapped_pages"] = {
                "total": len(flags),
                "valid": len(valid),
                "valid_share_count_histogram": dict(
                    Counter((flag >> 1) & 7 for flag in valid)
                ),
                "valid_shared_multiple": sum(
                    bool(flag & (1 << 15)) and ((flag >> 1) & 7) > 1
                    for flag in valid
                ),
                "valid_locked": sum(bool(flag & (1 << 22)) for flag in valid),
            }
            regions = []
            for offset in (0, SIZE // 2, SIZE - STEP):
                info = MemoryBasicInformation64()
                received = self.kernel.VirtualQueryEx(
                    handle,
                    base + offset,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
                if received != ctypes.sizeof(info):
                    message = (
                        f"VirtualQueryEx returned {received}, "
                        f"error={ctypes.get_last_error()}"
                    )
                    raise OSError(message)
                regions.append(
                    {
                        "offset": offset,
                        **{
                            name: getattr(info, name)
                            for name, _ in info._fields_
                        },
                    }
                )
            observed["virtual_query"] = regions
            observed["end_ns"] = time.perf_counter_ns()
            return observed
        finally:
            if not self.kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())


def metadata():
    import psutil

    root = Path.cwd().resolve()
    module = Path(psutil.__file__).resolve()
    extension = Path(psutil._psutil.__file__).resolve()
    assert module.is_relative_to(root)
    assert extension.is_relative_to(root)
    jit = getattr(sys, "_jit", None)
    jit_info = {"present": jit is not None}
    for name in ("is_available", "is_enabled", "is_active"):
        function = getattr(jit, name, None)
        jit_info[name] = function() if function else None
    return {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python_compiler": platform.python_compiler(),
        "python_build": platform.python_build(),
        "jit": jit_info,
        "python_jit_env": os.environ.get("PYTHON_JIT"),
        "sysconfig": {
            name: sysconfig.get_config_var(name)
            for name in (
                "Py_GIL_DISABLED",
                "CC",
                "CFLAGS",
                "CONFIG_ARGS",
                "SOABI",
                "Py_DEBUG",
                "Py_STATS",
                "PY_ENABLE_JIT",
                "Py_TIER2",
            )
        },
        "psutil_module": str(module),
        "extension": str(extension),
        "extension_sha256": hashlib.sha256(extension.read_bytes()).hexdigest(),
        "mmap_page_size": mmap.PAGESIZE,
    }


def probe(mode, destination=None):
    from tests import PYTHON_EXE
    from tests import PYTHON_EXE_ENV
    from tests import reap_children
    from tests import spawn_subproc
    from tests import wait_for_file

    assert mode in {"discarded", "checksum"}
    assert mmap.PAGESIZE == STEP
    api = WindowsAPI()
    outcome = {
        "mode": mode,
        "size": SIZE,
        "step": STEP,
        "metadata": metadata(),
        "complete": False,
    }

    def persist():
        if destination:
            Path(destination).write_text(
                json.dumps(outcome, indent=2), encoding="utf-8"
            )

    persist()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "mapping"
        flag = Path(directory) / "ready"
        info_path = Path(directory) / "child-info.json"
        control = Path(directory) / "verify"
        reply = Path(directory) / "reply.json"
        with path.open("wb") as file:
            file.write(b"\0" * SIZE)
        with path.open("r+b") as file:
            mapping = mmap.mmap(file.fileno(), SIZE)
        try:
            base = ctypes.addressof(ctypes.c_char.from_buffer(mapping))
            checksum = None
            if mode == "discarded":
                for offset in range(0, SIZE, STEP):
                    mapping[offset]
            else:
                checksum = 0
                for offset in range(0, SIZE, STEP):
                    checksum += mapping[offset]
                assert checksum == 0
            outcome["parent_checksum"] = checksum
            outcome["parent_before_child"] = api.snapshot(os.getpid(), base)
            child_touch = (
                "for i in range(0, len(m), 4096):\n    m[i]"
                if mode == "discarded"
                else (
                    "checksum = 0\nfor i in range(0, len(m), 4096):\n"
                    "    checksum += m[i]\nassert checksum == 0"
                )
            )
            # Same spawn/wait helpers and one child as the fixture. Metadata
            # and post-snapshot data handshake add diagnostic overhead.
            code = (
                textwrap.dedent(f"""
                import mmap, time, ctypes, json, os
                f = open({str(path)!r}, 'r+b')
                m = mmap.mmap(f.fileno(), 0)
                address = ctypes.addressof(ctypes.c_char.from_buffer(m))
                checksum = None
            """)
                + child_touch
                + "\n"
                + textwrap.dedent(f"""
                with open({str(info_path)!r}, 'w') as stream:
                    json.dump(dict(address=address, checksum=checksum), stream)
                open({str(flag)!r}, 'w').close()
                deadline = time.monotonic() + 60
                while not os.path.exists({str(control)!r}):
                    if time.monotonic() > deadline:
                        raise TimeoutError('data handshake did not arrive')
                    time.sleep(0.05)
                observed = m[0]
                assert observed == 73
                m[1] = 91
                with open({str(reply) + ".tmp"!r}, 'w') as stream:
                    json.dump(dict(
                        parent_value_seen=observed, child_value_written=91
                    ), stream)
                os.replace({str(reply) + ".tmp"!r}, {str(reply)!r})
                time.sleep(60)
            """)
            )
            outcome["child_executable"] = PYTHON_EXE
            outcome["launcher_env"] = {
                name: PYTHON_EXE_ENV.get(name)
                for name in ("PYTHONPATH", "__PYVENV_LAUNCHER__")
            }
            child = spawn_subproc(cmd=[PYTHON_EXE, "-c", code])
            # wait_for_file uses the fixture's timeout/readiness semantics.
            wait_for_file(str(flag), empty=True)
            child_info = json.loads(info_path.read_text())
            outcome["child_info"] = child_info
            outcome["parent_after_child"] = api.snapshot(os.getpid(), base)
            outcome["child_after_touch"] = api.snapshot(
                child.pid, child_info["address"]
            )
            persist()
            mapping[0] = 73
            control.touch()
            child_reply = wait_for_file(str(reply), delete=False)
            # wait_for_file returns file data; parse after its nonempty check.
            outcome["data_visibility"] = json.loads(child_reply)
            assert mapping[1] == 91
            with path.open("rb") as disk:
                assert disk.read(2) == bytes((73, 91))
            outcome["data_visibility"]["parent_and_disk_saw_child_write"] = (
                True
            )
            before = outcome["parent_before_child"]["psutil_memory_footprint"][
                "shared"
            ]
            after = outcome["parent_after_child"]["psutil_memory_footprint"][
                "shared"
            ]
            outcome["aggregate_shared_delta"] = after - before
            outcome["original_threshold"] = SIZE // 2
            outcome["meets_original_threshold"] = after - before >= SIZE // 2
            outcome["complete"] = True
            return outcome
        finally:
            error = sys.exc_info()[1]
            if error is not None:
                outcome["error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
            persist()
            reap_children()
            mapping.close()


if __name__ == "__main__":
    result = probe(sys.argv[1], sys.argv[2])
    print(
        json.dumps(
            {
                name: result[name]
                for name in (
                    "mode",
                    "aggregate_shared_delta",
                    "meets_original_threshold",
                    "data_visibility",
                )
            },
            indent=2,
        ),
        flush=True,
    )
