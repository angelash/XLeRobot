"""Build an offline Windows x64 phone-vision kit without user runtime data."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

REQUIREMENTS=['record3d==1.4.1','av==16.0.1','numpy==1.26.4','opencv-python-headless==4.11.0.86']
SHELL_FILES=['fabrication/assembly/debug_console/platform_app.py',
             'fabrication/assembly/debug_console/platform_catalog.py',
             'fabrication/assembly/debug_console/test_platform.py',
             'fabrication/assembly/debug_console/platform_static/studio.js',
             'fabrication/assembly/debug_console/platform_static/index.html']


def sha(data):return hashlib.sha256(data).hexdigest()


def powershell_json(script):
    encoded=base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    env={key:value for key,value in os.environ.items() if key.upper()!='PSMODULEPATH'}
    return json.loads(subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
                                              env=env,encoding='utf-8-sig',timeout=30))


def download(url,path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():return
    temporary=path.with_suffix(path.suffix+'.download')
    quote=lambda value:"'"+str(value).replace("'","''")+"'"
    code="$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';Invoke-WebRequest -Uri "+quote(url)+" -OutFile "+quote(temporary)+" -TimeoutSec 55"
    encoded=base64.b64encode(code.encode('utf-16-le')).decode('ascii')
    env={k:v for k,v in os.environ.items() if k.upper()!='PSMODULEPATH'}
    subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],env=env,check=True,timeout=60)
    temporary.replace(path)


def unzip(source,destination):
    destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        if sum(row.file_size for row in archive.infolist())>400_000_000:raise ValueError('Archive too large')
        for row in archive.infolist():
            target=(destination/row.filename).resolve()
            if not target.is_relative_to(destination):raise ValueError('Archive path outside target')
        archive.extractall(destination)


def prepare(output,wheelhouse,base_revision,apple_driver):
    repo=Path(__file__).resolve().parents[2]
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    if (output/'manifest.json').exists():raise ValueError('Choose a new output directory; do not overwrite a sealed kit')
    assets=output.parent/'phone-kit-assets';assets.mkdir(parents=True,exist_ok=True)
    python_zip=assets/'python-3.11.9-embed-amd64.zip'
    android_zip=assets/'platform-tools-windows.zip'
    scrcpy_zip=assets/'scrcpy-win64-v5.0.1.zip';checksums=assets/'scrcpy-SHA256SUMS.txt'
    downloads=[('https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip',python_zip),
               ('https://dl.google.com/android/repository/platform-tools-latest-windows.zip',android_zip),
               ('https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/scrcpy-win64-v5.0.1.zip',scrcpy_zip),
               ('https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/SHA256SUMS.txt',checksums)]
    for url,path in downloads:
        print('Preparing '+path.name,flush=True);download(url,path)
    expected=next(line.split()[0] for line in checksums.read_text().splitlines() if 'scrcpy-win64-v5.0.1.zip' in line)
    if sha(scrcpy_zip.read_bytes())!=expected:raise ValueError('Official scrcpy SHA256 mismatch')
    apple_driver=Path(apple_driver).resolve()
    driver_signature_script="[Console]::OutputEncoding=[Text.Encoding]::UTF8;$s=Get-AuthenticodeSignature -LiteralPath '"+str(apple_driver).replace("'","''")+"';[pscustomobject]@{status=$s.Status.ToString();signer=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
    apple_signature=powershell_json(driver_signature_script)
    if apple_signature['status']!='Valid' or 'Apple Inc' not in apple_signature['signer']:
        raise ValueError('Apple USB driver signature verification failed')
    unzip(python_zip,output/'python');unzip(android_zip,output/'tools')
    (output/'python/python311._pth').write_text('python311.zip\n.\nLib/site-packages\nimport site\n',encoding='ascii')
    ps_signature="[Console]::OutputEncoding=[Text.Encoding]::UTF8;$s=Get-AuthenticodeSignature -LiteralPath '"+str(output/'python/python.exe').replace("'","''")+"';[pscustomobject]@{status=$s.Status.ToString();signer=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
    signature=powershell_json(ps_signature)
    if signature['status']!='Valid' or 'Python Software Foundation' not in signature['signer']:
        raise ValueError('Portable Python signature verification failed')
    wheelhouse=Path(wheelhouse).resolve()
    subprocess.run([sys.executable,'-m','pip','install','--no-deps','--no-index','--only-binary=:all:',
                    '--platform','win_amd64','--python-version','311','--implementation','cp','--abi','cp311',
                    '--target',str(output/'python/Lib/site-packages'),'--find-links',str(wheelhouse),*REQUIREMENTS],check=True)
    with zipfile.ZipFile(scrcpy_zip) as archive:
        server=next(name for name in archive.namelist() if name.endswith('/scrcpy-server'))
        (output/'tools/scrcpy-server').write_bytes(archive.read(server))
    download('https://raw.githubusercontent.com/Genymobile/scrcpy/v5.0.1/LICENSE',output/'tools/scrcpy-LICENSE.txt')
    sources=SHELL_FILES+['docs/autonomy/PHONE_VISION_READY_20261109_ZH.md']+[str(path.relative_to(repo)).replace('\\','/') for path in sorted((repo/'deployment/phone_vision').rglob('*'))
                         if path.is_file() and path.suffix in ('.py','.js','.html','.css','.md','.ps1') and '__pycache__' not in path.parts]
    rows=[]
    for relative in sources:
        source=repo/relative;target=output/'source'/relative
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        original=subprocess.run(['git','-C',str(repo),'show',base_revision+':'+relative],capture_output=True)
        rows.append({'target':relative,'base_text_sha256':sha(original.stdout.replace(b'\r\n',b'\n')) if original.returncode==0 else None,
                     'text_sha256':sha(source.read_bytes().replace(b'\r\n',b'\n'))})
    shutil.copy2(repo/'deployment/phone_vision/install_kit.py',output/'install_kit.py')
    (output/'drivers').mkdir(exist_ok=True)
    shutil.copy2(apple_driver,output/'drivers/AppleMobileDeviceSupport64.msi')
    shutil.copy2(repo/'deployment/phone_vision/install_apple_driver.ps1',output/'install_apple_driver.ps1')
    (output/'INSTALL_APPLE_DRIVER.cmd').write_text('@echo off\r\ncd /d "%~dp0"\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File install_apple_driver.ps1\r\npause\r\n',encoding='ascii')
    (output/'INSTALL.cmd').write_text('@echo off\r\ncd /d "%~dp0"\r\npython\\python.exe install_kit.py --robot-root "D:\\Robot\\XLeRobot"\r\npause\r\n',encoding='ascii')
    (output/'README.txt').write_text('离线安装到已有 D:\\Robot\\XLeRobot。运行 INSTALL.cmd；遇源码不一致会停止，不覆盖其他修改。\n安装不重启平台、不读取手机、不发动作。依原流程重载 Studio 后，在“手机视觉”启用测试。\n首次手机信任／USB调试授权、Record3D开始采集仍需手机端操作。\n',encoding='utf-8')
    manifest={'format':1,'base_revision':base_revision,'source_files':rows,'dependencies':REQUIREMENTS,'python_signature':signature,
              'apple_usb_driver':{'source':'https://www.apple.com/itunes/download/win64','package':'AppleMobileDeviceSupport64.msi','signature':apple_signature,'installed':False},
              'downloads':[{'url':url,'sha256':sha(path.read_bytes())} for url,path in downloads],
              'files':[{'path':str(path.relative_to(output)).replace('\\','/'),'bytes':path.stat().st_size,'sha256':sha(path.read_bytes())}
                       for path in sorted(output.rglob('*')) if path.is_file() and '__pycache__' not in path.parts]}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    check=subprocess.run([str(output/'python/python.exe'),str(output/'source/deployment/phone_vision/worker.py'),'--check'],
                         capture_output=True,text=True,encoding='utf-8',timeout=30)
    if check.returncode:raise RuntimeError('Portable worker validation failed: '+check.stderr[:500])
    print(json.dumps({'kit':str(output),'files':len(manifest['files']),'dependencies':json.loads(check.stdout)},ensure_ascii=False),flush=True)
    return manifest


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True);parser.add_argument('--wheelhouse',required=True);parser.add_argument('--base-revision',required=True);parser.add_argument('--apple-driver',required=True)
    args=parser.parse_args();prepare(**vars(args))
