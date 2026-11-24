import asyncio
import shutil
import sys

import pytest

from artorias_device.tools.macos_python import MacOSVenvPythonBackend, sandbox_profile


def test_seatbelt_profile_keeps_runtime_offline_and_environment_read_only(tmp_path):
    runtime = tmp_path / 'venv'
    outputs = tmp_path / 'outputs'
    policy = sandbox_profile([runtime, outputs], [outputs], [runtime / 'bin/python'])
    rules = policy.splitlines()
    assert '(deny default)' in rules
    assert f'(allow file-read* (subpath "{runtime.resolve()}"))' in rules
    assert [rule for rule in rules if 'file-write*' in rule] == [
        f'(allow file-write* (subpath "{outputs.resolve()}"))',
    ]
    assert 'network-outbound' not in policy
    assert 'process-fork' not in policy


@pytest.mark.skipif(sys.platform != 'darwin' or not shutil.which('uv'), reason='requires macOS Seatbelt and uv')
def test_native_backend_stages_output_without_direct_write(tmp_path):
    backend = MacOSVenvPythonBackend(tmp_path / 'cache')
    root = tmp_path / 'execution'
    for directory in ('app', 'inputs', 'outputs'):
        (root / directory).mkdir(parents=True)
    external = tmp_path / 'original.txt'
    external.write_text('original')
    (root / 'app' / '__entry__.py').write_text(
        'from pathlib import Path\n'
        'import sys\n'
        'try:\n'
        '    Path(sys.argv[1]).write_text("unsafe")\n'
        'except PermissionError:\n'
        '    Path(sys.argv[2]).write_text("candidate")\n'
        'else:\n'
        '    raise AssertionError("External write succeeded")\n'
    )
    result = asyncio.run(backend.run({
        'id': 'native-test',
        'profile': backend.profile(),
        'request': {'mode': 'isolated', 'timeout_seconds': 10,
                    'args': [str(external), '/outputs/result.txt']},
    }, root))
    assert result['execution_status'] == 'succeeded', result
    assert external.read_text() == 'original'
    assert (root / 'outputs' / 'result.txt').read_text() == 'candidate'
