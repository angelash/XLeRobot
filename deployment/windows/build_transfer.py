"""Build a local transfer kit from reviewed tracked files; never include Git history."""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import zipfile

REPO=Path(__file__).resolve().parents[2]


def source_allowed(name):
    p=PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts:return False
    if any(x in ('.git','.ssh','.local-deploy','node_modules','__pycache__','evidence','platform-data') or x.startswith('.venv') for x in p.parts):return False
    if p.suffix.lower() in ('.pem','.key','.pfx','.p12','.log','.pid','.pyc'):return False
    if p.name.lower().startswith('.env') or p.name.lower() in ('id_rsa','id_ed25519','authorized_keys','known_hosts'):return False
    return True


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def zip_files(destination,files,root):
    names=[]
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for file in sorted(files):
            name=file.relative_to(root).as_posix()
            archive.write(file,name);names.append(name)
    with zipfile.ZipFile(destination) as archive:
        bad=archive.testzip()
        if bad:raise RuntimeError('ZIP CRC failure: '+bad)
    return names


def build(output,assets,public_key):
    output=Path(output).resolve();assets=Path(assets).resolve();public_key=Path(public_key).resolve()
    if output.exists():raise ValueError('Output already exists; select a new immutable kit directory')
    if subprocess.run(['git','diff','--quiet','HEAD','--'],cwd=REPO).returncode:raise ValueError('Commit reviewed source changes before building a versioned kit')
    key=public_key.read_text(encoding='utf-8').strip()
    if not key.startswith('ssh-ed25519 ') or '\n' in key or 'PRIVATE KEY' in key:raise ValueError('Only an Ed25519 public key can enter the kit')
    required=['installers/python-3.11.9-amd64.exe','tools/ffmpeg.exe','requirements-core.lock']
    for name in required:
        if not (assets/name).is_file():raise FileNotFoundError(assets/name)
    if not list((assets/'wheelhouse').glob('*.whl')):raise ValueError('No offline wheels available')
    output.mkdir(parents=True)
    tracked=subprocess.check_output(['git','ls-files','-z'],cwd=REPO).decode('utf-8').split('\0')
    included=[name for name in tracked if name and source_allowed(name) and (REPO/name).is_file()]
    excluded=[name for name in tracked if name and not source_allowed(name)]
    zip_files(output/'project.zip',[REPO/name for name in included],REPO)
    for name in ['installers','tools','wheelhouse','validation']:
        shutil.copytree(assets/name,output/name)
    for name in ['requirements-core.lock','assets-provenance.json','validation-result.json','validation-tests.xml']:
        if (assets/name).is_file():shutil.copy2(assets/name,output/name)
    remote=output/'remote';remote.mkdir()
    shutil.copy2(public_key,remote/'controller.pub')
    for name in ['enable-remote-server.ps1','collect-info.ps1','trust-robot.ps1','connect-robot.ps1']:
        shutil.copy2(REPO/'deployment/windows'/name,remote/name)
    shutil.copy2(REPO/'deployment/windows/install-kit.ps1',output/'install-kit.ps1')
    shutil.copy2(REPO/'deployment/windows/README_ZH.md',output/'README_FIRST.md')
    references=[]
    for name in ['fabrication/assembly/device-calibration.json','fabrication/assembly/kinematic-calibration.json']:
        if (REPO/name).is_file():references.append(REPO/name)
    library=REPO/'fabrication/assembly/debug_console/platform-data'
    if library.exists():
        references.extend(p for p in library.rglob('*') if p.is_file() and p.suffix in ('.json','.jsonl'))
    # Copies references only. The installer never places these in active calibration paths.
    zip_files(output/'runtime-reference.zip',references,REPO)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip()
    manifest={'created_at':datetime.now().astimezone().isoformat(),'source_commit':commit,
              'target':'Windows x64 / Python 3.11','source_files':len(included),'excluded_source_paths':excluded,
              'contains_private_ssh_key':False,'reference_files':len(references),
              'calibration_restore':'reference only; read fresh device registers before activation',
              'files':[{'path':p.relative_to(output).as_posix(),'bytes':p.stat().st_size,'sha256':digest(p)} for p in sorted(output.rglob('*')) if p.is_file()]}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    archive=output.with_name(output.name+'.zip')
    if archive.exists():raise ValueError('Outer ZIP already exists')
    zip_files(archive,[p for p in output.rglob('*') if p.is_file()],output.parent)
    checksum=digest(archive)
    archive.with_suffix('.zip.sha256').write_text(checksum+'  '+archive.name+'\n',encoding='ascii')
    return {'directory':str(output),'zip':str(archive),'bytes':archive.stat().st_size,'sha256':checksum,'source_commit':commit,'source_files':len(included),'reference_files':len(references),'excluded':excluded}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--assets',required=True);parser.add_argument('--public-key',required=True)
    args=parser.parse_args();print(json.dumps(build(args.output,args.assets,args.public_key),ensure_ascii=False,indent=2))
