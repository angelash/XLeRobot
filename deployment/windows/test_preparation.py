import zipfile
import json
import subprocess
import sys
import os
import base64
import re
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from deployment.windows.build_transfer import source_allowed,zip_files,digest


@pytest.mark.parametrize('name',['XLeVR/key.pem','.ssh/id_ed25519','.env','.env.local','fabrication/assembly/evidence/2026/control-events.jsonl','.local-deploy/preflight.json','fabrication/assembly/debug_console/platform-data/poses/a.json','a/../../key.txt'])
def test_source_archive_excludes_local_private_material(name):assert not source_allowed(name)


def test_source_archive_keeps_docs_scripts_and_illustrations():
    for name in ['PROJECT_STATE.md','deployment/windows/README_ZH.md','hardware/part.stl','fabrication/assembly/debug_console/platform_static/research/scores.png']:
        assert source_allowed(name)


def test_zip_keeps_directory_structure_and_roundtrips_hash(tmp_path):
    root=tmp_path/'source';(root/'nested').mkdir(parents=True);p=root/'nested/file.txt';p.write_text('robot',encoding='utf-8')
    dest=tmp_path/'project.zip';assert zip_files(dest,[p],root)==['nested/file.txt']
    with zipfile.ZipFile(dest) as z:assert z.read('nested/file.txt')==b'robot'
    assert len(digest(dest))==64


def test_ssh_tunnel_origin_is_allowed_but_foreign_origin_is_rejected(monkeypatch,tmp_path):
    from fabrication.assembly.debug_console import platform_app as mod
    monkeypatch.setattr(mod,'service',mod.Workbench(tmp_path))
    with TestClient(mod.app) as client:
        assert client.post('/api/platform/sandbox/reset',json={},headers={'Origin':'http://127.0.0.1:18770'}).status_code==200
        assert client.post('/api/platform/sandbox/reset',json={},headers={'Origin':'http://unknown.example:18770'}).status_code==403


@pytest.mark.skipif(sys.platform!='win32',reason='Windows PowerShell installer contract')
def test_windows_installer_checks_hashes_and_keeps_reference_calibration_inactive(tmp_path):
    import shutil
    kit=tmp_path/'kit with spaces';kit.mkdir()
    entry=Path(__file__).parent/'install-kit.ps1';shutil.copy2(entry,kit/entry.name)
    with zipfile.ZipFile(kit/'project.zip','w') as z:
        z.writestr('deployment/windows/bootstrap.ps1','param([string]$KitRoot,[switch]$Apply)\nWrite-Output "SIMULATED_BOOTSTRAP_OK"')
    with zipfile.ZipFile(kit/'runtime-reference.zip','w') as z:z.writestr('fabrication/assembly/device-calibration.json','{"historical":true}')
    manifest={'files':[{'path':p.name,'sha256':digest(p)} for p in kit.glob('*.zip')]}
    (kit/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    target=tmp_path/'new robot with spaces'
    command=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(kit/entry.name),'-Destination',str(target),'-Apply']
    env={k:v for k,v in os.environ.items() if k.upper()!='PSMODULEPATH'}
    result=subprocess.run(command,capture_output=True,env=env)
    assert result.returncode==0,result.stderr.decode(errors='replace')
    assert b'SIMULATED_BOOTSTRAP_OK' in result.stdout
    assert not (target/'fabrication/assembly/device-calibration.json').exists()
    (kit/'project.zip').write_bytes(b'tampered')
    target2=tmp_path/'must not extract';command[-2]=str(target2)
    assert subprocess.run(command,capture_output=True,env=env).returncode!=0
    assert not target2.exists()


@pytest.mark.skipif(sys.platform!='win32',reason='Windows PowerShell native argument transport')
def test_windows_bootstrap_native_python_probes():
    # Execute the actual installer probes across the real Windows PowerShell 5.1
    # boundary. Parsing the script alone misses stripped native-command quotes.
    script=(Path(__file__).parent/'bootstrap.ps1').read_text(encoding='utf-8')
    probes=re.findall(r"(& \$(?:PythonExe|targetPython) -c '[^'\r\n]*')",script)
    assert len(probes)==2
    setup="$ErrorActionPreference='Stop';$PythonExe='"+sys.executable.replace("'","''")+"';$targetPython=$PythonExe;"
    env={k:v for k,v in os.environ.items() if k.upper()!='PSMODULEPATH'}
    for probe in probes:
        code=setup+probe+';exit $LASTEXITCODE'
        encoded=base64.b64encode(code.encode('utf-16-le')).decode('ascii')
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],capture_output=True,env=env,timeout=30)
        assert result.returncode==0,result.stderr.decode(errors='replace')
        assert result.stdout.strip()
