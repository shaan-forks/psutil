"""Disposable same-runner baseline; preserves original source and assertions."""

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    output = Path(os.environ['BASELINE_OUTPUT'])
    output.mkdir(parents=True, exist_ok=True)
    workspace = Path(os.environ['GITHUB_WORKSPACE'])
    phase = os.environ.get('BASELINE_PHASE', 'baseline')
    assert phase in ('baseline', 'probe')
    results = []
    environments = {}
    environment = os.environ.copy()
    environment.update(
        PYTHONWARNINGS='always', PYTHONUNBUFFERED='1', PSUTIL_DEBUG='1',
        PSUTIL_TESTING='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
    )
    pytest_args = [
        '-m', 'pytest', '--durations=5',
        '-o', f'cache_dir={os.environ["BASELINE_CACHE"]}',
        '-p', 'no:instafail',
    ]
    constraints = output / 'constraints.txt'
    constraints.write_text(
        'setuptools==84.0.0\npytest==9.1.1\npytest-instafail==0.5.0\n'
        'pytest-xdist==3.8.0\n', encoding='utf-8'
    )

    def run(label, command, cwd):
        print(f'[{label}] {command!r}', flush=True)
        log = output / f'{label}.log'
        with log.open('w', encoding='utf-8') as stream:
            completed = subprocess.run(
                command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT,
                check=False, env=environment,
            )
        result = {'label': label, 'command': command, 'cwd': str(cwd),
                  'returncode': completed.returncode, 'log': log.name}
        results.append(result)
        print(f'[{label}] exit={completed.returncode}', flush=True)
        (output / 'results.json').write_text(json.dumps({
            'head_sha': os.environ['BASELINE_SHA'],
            'base_sha': 'ab9767553d8339e290c62364b38bd23e893408a6',
            'phase': phase,
            'results': results,
        }, indent=2), encoding='utf-8')
        return completed.returncode

    metadata = (
        'import json,platform,sys,sysconfig; '
        'print(json.dumps(dict(python=sys.version,executable=sys.executable,'
        'platform=platform.platform(),machine=platform.machine(),'
        'gil_disabled=sysconfig.get_config_var("Py_GIL_DISABLED")),indent=2)); '
        'assert platform.machine().upper() == "ARM64"; '
        'assert not sysconfig.get_config_var("Py_GIL_DISABLED")'
    )
    for version in ('314', '315'):
        source = workspace / f'source-{version}'
        interpreter = os.environ[f'PYTHON{version}']
        venv = Path(os.environ['RUNNER_TEMP']) / f'psutil-baseline-venv-{version}'
        version_metadata = metadata + f'; assert sys.version_info[:2] == (3, {version[1:]})'
        if run(f'{version}-environment', [interpreter, '-c', version_metadata], source):
            continue
        if run(f'{version}-venv', [interpreter, '-m', 'venv', str(venv)], source):
            continue
        python = str(venv / 'Scripts' / 'python.exe')
        if run(f'{version}-build-dependencies', [python, '-m', 'pip', 'install',
               '--constraint', str(constraints), 'setuptools'], source):
            continue
        # Match ci-install: install this source before psleak can pull psutil
        # from PyPI, and build independently in each checkout/venv.
        if run(f'{version}-build', [python, '-m', 'pip', 'install',
               '--no-build-isolation', '--editable', '.'], source):
            continue
        if run(f'{version}-dependencies', [python, '-m', 'pip', 'install',
               '--constraint', str(constraints), '--group', 'test'], source):
            continue
        run(f'{version}-freeze', [python, '-m', 'pip', 'freeze'], source)
        # Same Windows pywin32 cache warmup used by ci-install.
        if run(f'{version}-wmi-warmup', [python, '-c', 'import wmi'], source):
            continue
        if run(f'{version}-import', [python, '-c',
               'import psutil; from pathlib import Path; '
               'assert Path(psutil.__file__).resolve().is_relative_to(Path.cwd().resolve()); '
               'assert Path(psutil._psutil.__file__).resolve().is_relative_to(Path.cwd().resolve()); '
               'print(psutil.__file__); '
               'print(psutil._psutil.__file__); '
               'print(psutil.virtual_memory()); '
               'print(psutil.Process().memory_info()); '
               'print(psutil.Process().memory_footprint())'], source):
            continue
        environments[version] = (source, python)

    # Same physical runner, reversing order on alternating repetitions.
    # Focused tests are entirely unchanged; failures are retained, not retried away.
    target = 'tests/test_process.py::TestProcess::test_memory_footprint_shared'
    for repetition in range(1 if phase == 'probe' else 4):
        order = ('314', '315') if repetition % 2 == 0 else ('315', '314')
        for version in order:
            if version in environments:
                source, python = environments[version]
                run(f'{version}-focused-{repetition + 1}',
                    [python, *pytest_args, target], source)

    if phase == 'probe':
        for repetition in range(2):
            versions = ('314', '315') if repetition == 0 else ('315', '314')
            modes = ('discarded', 'checksum') if repetition == 0 else ('checksum', 'discarded')
            for version in versions:
                if version in environments:
                    source, python = environments[version]
                    for mode in modes:
                        label = f'{version}-probe-{mode}-{repetition + 1}'
                        run(label, [python, 'windows_memory_probe.py', mode,
                            str(output / f'{label}.json')], source)

        # Cross-load the same 3.14-built limited-API binary under 3.15 in a
        # separate copy. Neither original checkout/build is modified.
        if len(environments) == 2:
            source314, _ = environments['314']
            source315, python315 = environments['315']
            cross = Path(os.environ['RUNNER_TEMP']) / 'psutil-crossload-315'
            shutil.copytree(source315, cross, ignore=shutil.ignore_patterns(
                '.git', 'build', '*.egg-info', '__pycache__', '.pytest_cache',
            ))
            binaries314 = list((source314 / 'psutil').glob('_psutil*.pyd'))
            binaries315 = list((cross / 'psutil').glob('_psutil*.pyd'))
            assert len(binaries314) == len(binaries315) == 1
            original315_hash = hashlib.sha256(binaries315[0].read_bytes()).hexdigest()
            shutil.copy2(binaries314[0], binaries315[0])
            shared_hash = hashlib.sha256(binaries314[0].read_bytes()).hexdigest()
            assert hashlib.sha256(binaries315[0].read_bytes()).hexdigest() == shared_hash
            (output / 'crossload-provenance.json').write_text(json.dumps({
                'source314_binary': str(binaries314[0]),
                'cross315_binary': str(binaries315[0]),
                'shared_binary_sha256': shared_hash,
                'original315_binary_sha256': original315_hash,
            }, indent=2), encoding='utf-8')
            cross_gate = (
                'import psutil,hashlib; from pathlib import Path; '
                'root=Path.cwd().resolve(); '
                'module=Path(psutil.__file__).resolve(); '
                'extension=Path(psutil._psutil.__file__).resolve(); '
                'assert module.is_relative_to(root); '
                'assert extension.is_relative_to(root); '
                'digest=hashlib.sha256(extension.read_bytes()).hexdigest(); '
                f'assert digest == {shared_hash!r}; '
                'print(module); print(extension); print(digest)'
            )
            if run('315-crossload-import', [python315, '-c', cross_gate], cross) == 0:
                run('315-crossload-focused', [python315, *pytest_args, target], cross)
                for mode in ('discarded', 'checksum'):
                    label = f'315-crossload-probe-{mode}'
                    run(label, [python315, 'windows_memory_probe.py', mode,
                        str(output / f'{label}.json')], cross)
    else:
        # Full-suite baseline was already preserved by the first diagnostic run.
        for version in ('314', '315'):
            if version in environments:
                source, python = environments[version]
                run(f'{version}-full-suite',
                    [python, *pytest_args, '-n', 'auto',
                     '--dist', 'loadgroup', '-m', 'not isolated'], source)
                run(f'{version}-isolated-suite',
                    [python, *pytest_args, '-m', 'isolated'], source)

    # Report actual failures to Actions; artifact upload runs with always().
    return 1 if len(environments) != 2 or any(r['returncode'] for r in results) else 0


if __name__ == '__main__':
    sys.exit(main())
