"""Temporary Windows ARM64 mmap accounting probe."""

import mmap
import os
import subprocess
import sys
import tempfile

import psutil

SIZE = 32 * 1024 * 1024
STEP = mmap.PAGESIZE


def shared(pid=None):
    return psutil.Process(pid).memory_footprint().shared


def probe(access):
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        path = tmp.name
        tmp.write(b"\0" * SIZE)
    child = None
    mapping = None
    try:
        with open(path, "r+b") as file:
            mapping = mmap.mmap(file.fileno(), SIZE, access=access)
            if access == mmap.ACCESS_WRITE:
                for i in range(0, SIZE, STEP):
                    mapping[i] = 1
            else:
                checksum = sum(mapping[i] for i in range(0, SIZE, STEP))
                print("parent read checksum:", checksum, flush=True)
            before = shared()
            code = """
import mmap, sys, time
path, access, step = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
with open(path, 'r+b') as file:
    mapping = mmap.mmap(file.fileno(), 0, access=access)
    if access == mmap.ACCESS_WRITE:
        for i in range(0, len(mapping), step):
            mapping[i] = 2
    else:
        checksum = sum(mapping[i] for i in range(0, len(mapping), step))
        print('child read checksum:', checksum, flush=True)
    print('ready', flush=True)
    time.sleep(30)
"""
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    code,
                    path,
                    str(access),
                    str(STEP),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            while True:
                line = child.stdout.readline()
                if not line:
                    raise RuntimeError(child.stderr.read())
                print(line.rstrip(), flush=True)
                if line.strip() == "ready":
                    break
            after = shared()
            child_shared = shared(child.pid)
            visibility = [mapping[i] for i in (0, SIZE // 2, SIZE - STEP)]
            with open(path, "rb") as file2:
                disk = [file2.read(1)[0]]
                file2.seek(SIZE // 2)
                disk.append(file2.read(1)[0])
            print(
                f"access={access} before={before} after={after} "
                f"delta={after - before} child={child_shared} "
                f"parent_sees={visibility} disk={disk}",
                flush=True,
            )
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=10)
        if mapping is not None:
            mapping.close()
        os.unlink(path)


print(sys.version, flush=True)
probe(mmap.ACCESS_WRITE)
probe(mmap.ACCESS_READ)
