"""Fetch offline Windows x64 dependencies; does not run any installer."""
import argparse
import os
from email.parser import BytesParser
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from deployment.windows.build_transfer import digest

DOWNLOADS={
    'installers/python-3.11.9-amd64.exe':'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe',
    'validation/python-3.11.9-embed-amd64.zip':'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip',
    'installers/VC_redist.x64.exe':'https://aka.ms/vs/17/release/vc_redist.x64.exe',
}


def prepare(output,ffmpeg):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    provenance={'target':'Windows x64, CPython 3.11','downloads':[],'installers_executed':False}
    for relative,url in DOWNLOADS.items():
        path=output/relative;path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():
            request=urllib.request.Request(url,headers={'User-Agent':'XLeRobot-transfer-preparation'})
            with urllib.request.urlopen(request,timeout=40) as response,path.with_suffix('.download').open('wb') as file:
                shutil.copyfileobj(response,file)
            path.with_suffix('.download').replace(path)
        provenance['downloads'].append({'path':relative,'source_url':url,'bytes':path.stat().st_size,'sha256':digest(path)})
        print('Prepared '+relative,flush=True)
    if sys.platform=='win32':
        paths=','.join("'"+str(output/name).replace("'","''")+"'" for name in DOWNLOADS if name.endswith('.exe'))
        script="[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; @("+paths+") | ForEach-Object { $s=Get-AuthenticodeSignature $_; [pscustomobject]@{file=(Split-Path $_ -Leaf);status=$s.Status.ToString();signer=$s.SignerCertificate.Subject} } | ConvertTo-Json"
        env={k:v for k,v in os.environ.items() if k.upper()!='PSMODULEPATH'}
        checked=subprocess.check_output(['powershell.exe','-NoProfile','-Command',script],env=env,encoding='utf-8-sig')
        provenance['authenticode']=json.loads(checked)
        if any(item['status']!='Valid' for item in provenance['authenticode']):raise RuntimeError('Installer signature verification did not pass')
    wheels=output/'wheelhouse';wheels.mkdir(exist_ok=True)
    root=Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable,'-m','pip','--isolated','download','--index-url','https://pypi.org/simple',
                    '--only-binary=:all:','--platform','win_amd64','--python-version','311','--implementation','cp','--abi','cp311',
                    '-r',str(root/'fabrication/assembly/debug_console/requirements.txt'),'-d',str(wheels)],check=True)
    packages={}
    for wheel in sorted(wheels.glob('*.whl')):
        with zipfile.ZipFile(wheel) as z:
            metadata=BytesParser().parsebytes(z.read(next(n for n in z.namelist() if n.endswith('.dist-info/METADATA'))))
            packages[metadata['Name']]=metadata['Version']
    (output/'requirements-core.lock').write_text('\n'.join(f'{k}=={v}' for k,v in sorted(packages.items(),key=lambda x:x[0].lower()))+'\n',encoding='ascii')
    tools=output/'tools';tools.mkdir(exist_ok=True)
    shutil.copy2(ffmpeg,tools/'ffmpeg.exe')
    for name in ['LICENSE','LICENSE.txt']:
        candidate=Path(ffmpeg).parent.parent/name
        if candidate.is_file():shutil.copy2(candidate,tools/name)
    provenance['ffmpeg']={'version':subprocess.check_output([str(ffmpeg),'-version'],text=True,encoding='utf-8').splitlines()[0],
                          'sha256':digest(tools/'ffmpeg.exe'),'source':'existing local WinGet Gyan.FFmpeg distribution'}
    provenance['packages']=packages
    (output/'assets-provenance.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'assets':str(output),'wheels':len(packages),'ffmpeg_bytes':(tools/'ffmpeg.exe').stat().st_size}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--ffmpeg',required=True)
    args=parser.parse_args();prepare(args.output,args.ffmpeg)
