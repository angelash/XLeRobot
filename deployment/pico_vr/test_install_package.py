"""Exercise the real PowerShell installer in disposable directories.

All HTTP, process and scheduled-task operations are replaced in the child shell.
The test Python executable only returns scripted exit codes; it never opens a bus.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from fabrication.assembly.debug_console import studio_cert

HERE = Path(__file__).resolve().parent
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell installer')

STUB = r'''
using System;
public class TestPython {
    public static int Main(string[] args) {
        string scenario = Environment.GetEnvironmentVariable("XLE_VR_TEST_SCENARIO");
        if (args.Length > 0 && args[0] == "-m") {
            Console.WriteLine("Scripted sandbox regression");
            return scenario == "regression-fail" ? 3 : 0;
        }
        Console.WriteLine("200");
        return scenario == "https-fail" ? 4 : 0;
    }
}
'''

HARNESS = r'''
param([string]$Installer, [string]$Repo, [switch]$Apply, [switch]$RestartController)
$ErrorActionPreference = 'Stop'
$script:StatusReads = 0
function Invoke-RestMethod {
    param([string]$Uri, [int]$TimeoutSec)
    if ($Uri -like '*/api/status') {
        $script:StatusReads++
        $mode = if ($env:XLE_VR_TEST_SCENARIO -eq 'late-takeover' -and $script:StatusReads -gt 1) { 'online' } else { 'offline' }
        return [pscustomobject]@{controller=@{controller=$mode};web_control=@{active=$false};
            agent_run=@{running=$false};limit_selftest=@{running=($env:XLE_VR_TEST_SCENARIO -eq 'servo-busy')}}
    }
    if ($Uri -like '*/api/platform/status') {
        if ($env:XLE_VR_TEST_SCENARIO -eq 'studio-missing') { throw 'offline' }
        return [pscustomobject]@{recording=$null}
    }
    if ($Uri -like '*/api/web/status') { return [pscustomobject]@{mode='vr'} }
    throw "Unexpected HTTP $Uri"
}
function Get-NetTCPConnection { param($LocalPort,$State,$ErrorAction) @() }
function Invoke-CimMethod {
    param($ClassName,$MethodName,$Arguments)
    Add-Content -LiteralPath (Join-Path $Repo 'service-calls.txt') -Value 'start-studio'
    return [pscustomobject]@{ReturnValue=0}
}
function Start-ScheduledTask {
    param($TaskName)
    if ($TaskName -ne 'XLeRobot-Boot') { throw 'Unexpected task' }
    Add-Content -LiteralPath (Join-Path $Repo 'service-calls.txt') -Value 'start-controller'
}
function Stop-Process { throw 'Tests must never stop a real process' }
function Get-CimInstance { throw 'Tests must never query a real process' }
function Start-Sleep { param($Milliseconds) }
function Copy-Item {
    param([string]$LiteralPath,[string]$Destination,[switch]$Force,[switch]$Recurse)
    $partial = $env:XLE_VR_TEST_PARTIAL
    if ($partial -and $LiteralPath -like '*\payload\*' -and $LiteralPath.EndsWith($partial)) {
        $env:XLE_VR_TEST_PARTIAL = ''
        [System.IO.File]::WriteAllText($Destination,'interrupted copy')
        throw 'Injected partial copy failure'
    }
    Microsoft.PowerShell.Management\Copy-Item @PSBoundParameters
}
try {
    & $Installer -Repo $Repo -Apply:$Apply -RestartController:$RestartController
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
} catch { Write-Output $_.Exception.Message; exit 1 }
'''


def ps(script, *args, env=None):
    # Python does not perform pwsh's native-child PSModulePath cleanup. Loading
    # PowerShell 7 modules into Windows PowerShell 5.1 hides Get-FileHash.
    child_env = dict(os.environ if env is None else env)
    child_env['PSMODULEPATH'] = os.pathsep.join([
        str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/Modules'),
        str(Path(os.environ['ProgramFiles']) / 'WindowsPowerShell/Modules')])
    return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                           '-ExecutionPolicy', 'Bypass', '-File', str(script), *map(str, args)],
                          capture_output=True, text=True, encoding='utf-8', errors='replace',
                          env=child_env, timeout=45)


@pytest.fixture(scope='module')
def toolkit(tmp_path_factory):
    root = tmp_path_factory.mktemp('pico-installer-tools')
    exe = root / 'test-python.exe'
    csharp = root / 'stub.cs'; csharp.write_text(STUB, encoding='utf-8')
    compile_script = root / 'compile.ps1'
    compile_script.write_text("param($Source,$Output)\n$ErrorActionPreference='Stop'\n"
                              "Add-Type -Path $Source -OutputAssembly $Output -OutputType ConsoleApplication\n",
                              encoding='utf-8')
    result = ps(compile_script, csharp, exe)
    assert result.returncode == 0, result.stdout + result.stderr
    harness = root / 'harness.ps1'; harness.write_text(HARNESS, encoding='utf-8')
    studio_cert.create(root / 'tls', addresses=['127.0.0.1'])
    return root, exe, harness


@pytest.fixture
def sandbox(tmp_path, toolkit):
    tools, exe, harness = toolkit
    repo, package = tmp_path / 'vehicle', tmp_path / 'package'
    package.mkdir(); entries = []
    for name in ('platform_app.py', 'web_teleop.py', 'vr_kinematics.py'):
        path = 'fabrication/assembly/debug_console/' + name
        data, before = ('updated ' + name).encode(), ('original ' + name).encode()
        source = package / 'payload' / path
        source.parent.mkdir(parents=True, exist_ok=True); source.write_bytes(data)
        accepted = []
        if name != 'vr_kinematics.py':
            target = repo / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(before)
            accepted.append(hashlib.sha256(before).hexdigest())
        entries.append({'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'accepted': accepted})
    (package / 'manifest.json').write_text(json.dumps({'files': entries}), encoding='utf-8')
    shutil.copy2(HERE / 'install-vehicle.ps1', package / 'install-pico-vr.ps1')
    (package / 'studio-tls').mkdir()
    for name in ('server.crt', 'server.key', 'ca.crt'):
        shutil.copy2(tools / 'tls' / name, package / 'studio-tls' / name)
    python = repo / '.venv-debug/Scripts/python.exe'; python.parent.mkdir(parents=True); shutil.copy2(exe, python)
    local = repo / '.local-deploy'; local.mkdir()
    (local / 'studio-network.json').write_text(json.dumps({
        'public_origin': 'http://192.168.137.150:8770', 'allowed_origins': ['http://192.168.137.150:8770'],
        'client_addresses': ['192.168.137.1']}), encoding='utf-8')
    (local / 'safety-holds.json').write_text('{"right_shoulder":true,"latched_stop":true}', encoding='utf-8')
    (local / 'studio-tls').mkdir()
    (local / 'studio-tls/server.key').write_bytes(b'previous test key')
    (repo / 'fabrication/assembly/debug_console/stop.ps1').write_text(
        'Add-Content -LiteralPath "' + str(repo / 'service-calls.txt') + '" -Value stop-controller', encoding='utf-8')
    return repo, package, harness, entries


def snapshot(repo):
    return {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob('*')
            if p.is_file() and 'pico-vr-install-' not in p.as_posix() and p.name != 'service-calls.txt'}


def install(sandbox, *, apply=True, controller=False, scenario='', partial=''):
    repo, package, harness, _ = sandbox
    args = ['-Installer', package / 'install-pico-vr.ps1', '-Repo', repo]
    if apply: args.append('-Apply')
    if controller: args.append('-RestartController')
    return ps(harness, *args, env={**os.environ, 'XLE_VR_TEST_SCENARIO': scenario, 'XLE_VR_TEST_PARTIAL': partial})


def test_plan_is_read_only(sandbox):
    repo, _, _, _ = sandbox; before = snapshot(repo)
    result = install(sandbox, apply=False)
    assert result.returncode == 0 and 'Plan only' in result.stdout
    assert snapshot(repo) == before
    assert not list((repo / '.local-deploy').glob('pico-vr-install-*'))


@pytest.mark.parametrize('scenario,reason', [('conflict', 'Concurrent source change'),
    ('servo-busy', 'limit test or servo session is active'),
    ('studio-missing', 'recording state is unknown'), ('path-escape', 'Path outside update root')])
def test_conflicts_activity_and_unknown_recording_refuse_before_changes(sandbox, scenario, reason):
    repo, package, _, entries = sandbox
    if scenario == 'conflict': (repo / entries[0]['path']).write_bytes(b'other conversation')
    if scenario == 'path-escape':
        entries[0]['path'] = '../../outside.py'
        (package / 'manifest.json').write_text(json.dumps({'files': entries}), encoding='utf-8')
    before = snapshot(repo)
    result = install(sandbox, scenario=scenario, controller=True)
    assert result.returncode != 0 and reason in result.stdout
    assert snapshot(repo) == before
    assert not (repo / 'service-calls.txt').exists()


@pytest.mark.parametrize('partial', ['web_teleop.py', 'vr_kinematics.py'])
def test_interrupted_copy_restores_overwritten_or_new_file_without_restart(sandbox, partial):
    repo, _, _, _ = sandbox; before = snapshot(repo)
    result = install(sandbox, partial=partial, controller=True)
    assert result.returncode != 0 and 'Injected partial copy failure' in result.stdout
    assert snapshot(repo) == before
    assert not (repo / 'service-calls.txt').exists()


def test_failed_regression_restores_files_network_and_tls_without_restart(sandbox):
    repo, _, _, _ = sandbox; before = snapshot(repo)
    result = install(sandbox, scenario='regression-fail', controller=True)
    assert result.returncode != 0 and 'Vehicle regression tests failed' in result.stdout
    assert snapshot(repo) == before
    assert not (repo / 'service-calls.txt').exists()


def test_failed_https_restarts_previous_controller_only_after_attempted_restart(sandbox):
    repo, _, _, _ = sandbox; before = snapshot(repo)
    result = install(sandbox, scenario='https-fail', controller=True)
    assert result.returncode != 0 and 'HTTPS check failed' in result.stdout
    assert snapshot(repo) == before
    assert (repo / 'service-calls.txt').read_text().splitlines() == [
        'stop-controller', 'start-controller', 'stop-controller', 'start-controller']


def test_takeover_started_during_regression_prevents_restart_and_restores_files(sandbox):
    repo, _, _, _ = sandbox; before = snapshot(repo)
    result = install(sandbox, scenario='late-takeover', controller=True)
    assert result.returncode != 0 and 'Not idle before restart' in result.stdout
    assert snapshot(repo) == before
    assert not (repo / 'service-calls.txt').exists()


def test_success_copies_checked_payload_preserves_holds_and_enables_same_port_tls(sandbox):
    repo, package, _, entries = sandbox
    holds = (repo / '.local-deploy/safety-holds.json').read_bytes()
    result = install(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    for entry in entries:
        assert hashlib.sha256((repo / entry['path']).read_bytes()).hexdigest() == entry['sha256']
    config = json.loads((repo / '.local-deploy/studio-network.json').read_text(encoding='utf-8-sig'))
    assert config['allowed_origins'] == ['http://192.168.137.150:8770', 'https://192.168.137.150:8770']
    assert config['client_addresses'] == ['192.168.137.1'] and 'tls' in config
    assert (repo / '.local-deploy/studio-tls/server.key').read_bytes() == (package / 'studio-tls/server.key').read_bytes()
    assert (repo / '.local-deploy/safety-holds.json').read_bytes() == holds
    assert (repo / 'service-calls.txt').read_text().splitlines() == ['start-studio']
