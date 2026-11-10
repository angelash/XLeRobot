"""Install a verified phone-vision kit into an existing Windows Studio runtime.

No service restart, device access, global packages or controller modifications.
Existing source must match the recorded base or this exact update.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def text_digest(path):return hashlib.sha256(Path(path).read_bytes().replace(b'\r\n',b'\n')).hexdigest()


def inside(root,relative):
    path=(root/relative).resolve()
    if not path.is_relative_to(root.resolve()):raise ValueError('Kit path leaves its root')
    return path


def install(kit,robot_root):
    kit=Path(kit).resolve();robot_root=Path(robot_root).resolve()
    marker=robot_root/'fabrication/assembly/debug_console/platform_app.py'
    if not marker.is_file():raise ValueError('目标不是已有 Studio 运行目录')
    manifest=json.loads((kit/'manifest.json').read_text(encoding='utf-8'))
    config_path=robot_root/'.local-deploy/phone-vision.json'
    existing=json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
    if not isinstance(existing,dict):raise ValueError('已有采集配置格式无效，未修改目标文件')
    # Verify every packaged byte and every target before changing any file.
    for row in manifest['files']:
        path=inside(kit,row['path'])
        if not path.is_file() or path.stat().st_size!=row['bytes'] or digest(path)!=row['sha256']:
            raise ValueError('安装包校验失败：'+row['path'])
    for row in manifest['source_files']:
        target=inside(robot_root,row['target'])
        if target.exists() and text_digest(target) not in (row['base_text_sha256'],row['text_sha256']):
            raise ValueError('目标已有其他修改，请先合并，不覆盖：'+row['target'])
    result=subprocess.run([str(kit/'python/python.exe'),str(kit/'source/deployment/phone_vision/worker.py'),'--check'],
                          capture_output=True,text=True,encoding='utf-8',timeout=20)
    if result.returncode:raise ValueError('离线采集环境校验失败；未修改目标源码')
    # Prepare immutable native dependencies before touching the Studio source.
    version=digest(kit/'manifest.json')[:12]
    runtime=robot_root/'.local-deploy/phone-vision-runtimes'/version
    runtime.mkdir(parents=True,exist_ok=True)
    for row in manifest['files']:
        if row['path'].startswith(('python/','tools/')):
            source=inside(kit,row['path']);target=inside(runtime,row['path'])
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
            if digest(target)!=row['sha256']:raise ValueError('运行依赖复制校验失败')
    now=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup=robot_root/'.local-deploy/phone-vision-backups'/now
    backup.mkdir(parents=True)
    installed=[]
    try:
        for row in manifest['source_files']:
            source=inside(kit,'source/'+row['target']);target=inside(robot_root,row['target'])
            existed=target.exists()
            if existed:
                saved=inside(backup,row['target']);saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,saved)
            target.parent.mkdir(parents=True,exist_ok=True)
            temporary=target.with_name(target.name+'.phone-update')
            shutil.copy2(source,temporary);temporary.replace(target)
            installed.append({'path':row['target'],'existed':existed,'sha256':digest(target)})
    except Exception:
        for row in reversed(installed):
            target=inside(robot_root,row['path'])
            if target.is_file() and digest(target)==row['sha256']:
                if row['existed']:shutil.copy2(inside(backup,row['path']),target)
                else:target.unlink()
        raise
    config_updated=not existing or existing.get('managed_by')=='phone-vision-kit'
    if config_updated:
        if config_path.exists():shutil.copy2(config_path,backup/'phone-vision-config.json')
        cfg={'managed_by':'phone-vision-kit','python':str(runtime/'python/python.exe'),
             'adb':str(runtime/'tools/platform-tools/adb.exe'),'server':str(runtime/'tools/scrcpy-server'),
             'camera_id':existing.get('camera_id','0'),
             'android_transport':existing.get('android_transport','usb'),
             'android_endpoint':existing.get('android_endpoint',''),
             'android_role':existing.get('android_role','observer'),
             'android_rotation':existing.get('android_rotation','0'),
             'android_mode':existing.get('android_mode','camera'),
             'android_cameras':existing.get('android_cameras',[{'id':'0','label':'后置主摄'}]),
             'iphone_transport':existing.get('iphone_transport','usb'),
             'iphone_origin':existing.get('iphone_origin',''),'iphone_python':existing.get('iphone_python','')}
        temporary=config_path.with_suffix('.pending')
        temporary.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(config_path)
    receipt={'installed_at':now,'robot_root':str(robot_root),'backup':str(backup),'source_files':installed,
             'native_dependencies':json.loads(result.stdout),'service_restarted':False,'robot_commands':0,
             'config_updated':config_updated,'runtime':str(runtime)}
    (backup/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    return receipt


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--robot-root',required=True)
    args=parser.parse_args()
    print(json.dumps(install(Path(__file__).resolve().parent,args.robot_root),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
