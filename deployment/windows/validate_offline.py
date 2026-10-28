"""Validate downloaded CP311 wheels in an isolated portable Python, without installation."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid
import zipfile
import xml.etree.ElementTree as ET


def unpack_wheel(wheel,target):
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            relative=Path(name)
            if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe wheel path')
            if name.endswith('/'):continue
            # Module imports need package files, not generated console launchers.
            if len(relative.parts)>2 and relative.parts[0].endswith('.data'):
                if relative.parts[1] not in ('purelib','platlib'):continue
                relative=Path(*relative.parts[2:])
            path=target/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(archive.read(name))


def validate(assets):
    assets=Path(assets).resolve();repo=Path(__file__).resolve().parents[2]
    root=repo/'.local-deploy'/('validation311-'+uuid.uuid4().hex[:8]);root.mkdir(parents=True)
    with zipfile.ZipFile(assets/'validation/python-3.11.9-embed-amd64.zip') as archive:archive.extractall(root)
    site=root/'Lib/site-packages';site.mkdir(parents=True)
    validation=assets/'validation/wheels';validation.mkdir(parents=True,exist_ok=True)
    if not list(validation.glob('pytest-*.whl')):
        subprocess.run([sys.executable,'-m','pip','--isolated','download','--index-url','https://pypi.org/simple','--only-binary=:all:',
                        '--platform','win_amd64','--python-version','311','--implementation','cp','--abi','cp311','-d',str(validation),'pytest>=8,<9'],check=True)
    for wheel in list((assets/'wheelhouse').glob('*.whl'))+list(validation.glob('*.whl')):unpack_wheel(wheel,site)
    (root/'python311._pth').write_text('python311.zip\n.\nLib/site-packages\n'+str(repo)+'\nimport site\n',encoding='utf-8')
    python=root/'python.exe'
    probe='import sys,cv2,numpy,fastapi,httpx,PIL,serial;print(sys.version);print(dict(cv2=cv2.__version__,numpy=numpy.__version__,fastapi=fastapi.__version__,httpx=httpx.__version__))'
    subprocess.run([str(python),'-X','utf8','-c',probe],cwd=repo,check=True)
    tests=['fabrication/assembly/debug_console/test_control_runtime.py','fabrication/assembly/debug_console/test_original_teleop.py',
           'fabrication/assembly/debug_console/test_platform.py','fabrication/assembly/debug_console/test_serial_identity.py','deployment/windows/test_preparation.py']
    junit=assets/'validation-tests.xml'
    subprocess.run([str(python),'-X','utf8','-m','pytest',*tests,'-q','--junitxml='+str(junit)],cwd=repo,check=True)
    suite=ET.parse(junit).getroot().find('testsuite')
    result={'python':str(python),'validated_on':'current Windows x64 computer, portable CPython 3.11.9',
            'installer_executed':False,'serial_opened':False,'motor_writes':False,'new_robot_laptop_tested':False,'tests':tests,
            'test_counts':{k:int(suite.attrib[k]) for k in ['tests','failures','errors','skipped']}}
    (assets/'validation-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--assets',required=True);validate(parser.parse_args().assets)
