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
    assert '(allow file-read-data (literal "/"))' in rules
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


@pytest.mark.skipif(sys.platform != 'darwin' or not shutil.which('uv'), reason='requires macOS Seatbelt and uv')
def test_native_backend_executes_inline_code(tmp_path):
    from test_python_execution import execute, setup

    backend = MacOSVenvPythonBackend(tmp_path / 'envs')
    workspace, service, _ = setup(tmp_path, backend)
    plan = service.prepare(workspace, 'inline', {'code': 'print("INLINE_OK", sum(range(1, 11)))'})
    result = execute(service, workspace, plan)
    assert result['execution_status'] == 'succeeded', result
    assert result['stdout_tail'].strip() == 'INLINE_OK 55'


@pytest.mark.parametrize(
    'failed_step,expected_phase,expected_code',
    [
        ('create_venv', 'prepare_environment', 'process_start_failed'),
        ('resolve_dependencies', 'prepare_environment', 'nonzero_exit'),
        ('install_dependencies', 'prepare_environment', 'timeout'),
        ('run_python', 'execute_script', 'nonzero_exit'),
    ],
)
def test_native_failure_stage_reaches_tool_result(tmp_path, monkeypatch, failed_step, expected_phase, expected_code):
    from artorias_device.tools.macos_python import ProcessTimeout
    from test_python_execution import execute, setup

    backend = MacOSVenvPythonBackend(tmp_path / 'cache')
    monkeypatch.setattr(backend, 'profile', lambda: {
        'python': sys.executable, 'uv': '/some/uv', 'python_prefix': sys.base_prefix,
        'backend': 'macos-venv', 'location': 'device',
    })
    phases = iter(['create_venv', 'resolve_dependencies', 'install_dependencies', 'run_python'])
    calls = []

    async def mock_process(*args, **kwargs):
        step = next(phases)
        calls.append(step)
        if step != failed_step:
            return 0, '', '', False
        if expected_code == 'timeout':
            raise ProcessTimeout('resolving', 'while downloading wheel', False)
        if expected_code == 'process_start_failed':
            raise FileNotFoundError('sandbox launcher missing')
        return 1, '', 'diagnostic detail token=example-secret', False

    monkeypatch.setattr(backend, '_run_process', mock_process)
    ws, service, _ = setup(tmp_path, backend)
    plan = service.prepare(ws, 'native', {'path': 'script.py', 'dependencies': ['testpkg']})
    result = execute(service, ws, plan)
    assert result['phase'] == expected_phase
    assert result['step'] == failed_step
    assert result['error_code'] == expected_code
    assert calls[-1] == failed_step
    assert result['script_attempted'] == ('run_python' in calls)
    assert result['publish_status'] == 'not_requested'
    assert not result['published']
    assert result['logs_ref']
    logs = (tmp_path / 'data' / 'executions' / result['logs_ref']).read_text()
    assert result['stderr_tail'] in logs
    assert 'example-secret' not in logs
    if expected_code == 'timeout':
        assert result['execution_status'] == 'timed_out'
        assert result['stderr_tail'] == 'while downloading wheel'
    if expected_phase == 'prepare_environment':
        assert not list(backend.cache.iterdir())
