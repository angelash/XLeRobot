"""Add observed powered A/B/C references without changing servo coordinates.

Run on the robot with a reviewed manifest of successful agent snapshot steps.
This tool talks only to the existing control service. It never opens serial,
changes limits/homing, releases an arm or starts gamepad takeover.
"""
from copy import deepcopy
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import urllib.request

from .control_runtime import EXPECTED_SERIALS, now
from .upstream_teleop import O1, O2
from .whole_calibration import atomic_json


def build_references(profile, captures, arm_ports, motors):
    if profile.get('serials') != EXPECTED_SERIALS:
        raise ValueError('标定USB身份不匹配')
    if profile.get('full_manual_range_verified') is not True:
        raise ValueError('先完成实际行程自检或采集')
    if set(arm_ports.values()) != {'COM3', 'COM4'}:
        raise ValueError('双臂物理板映射不唯一')
    result = deepcopy(profile)
    for side, port in arm_ports.items():
        cards = [captures[side+'_'+name] for name in ('a','b','c','open','closed')]
        if any(card.get('port') != port for card in cards):
            raise ValueError('姿态卡与当前物理控制板不一致')
        for i in range(1,7):
            key = f'{port}:{i}'; entry = result['joints'][key]; motor = motors[key]
            if not (entry.get('range_verified') or entry.get('full_range_confirmed')):
                raise ValueError(key+'没有已确认行程')
            if any(entry.get(k) != motor[k] for k in ('min','max','homing_raw')):
                raise ValueError(key+'标定坐标或限位已变化')
            if motor['mode'] != 0 or not motor['min'] <= motor['position'] <= motor['max']:
                raise ValueError(key+'模式或当前位置不符')
            for card in cards:
                value = card['positions'][key]
                if not isinstance(value, int) or not entry['min'] <= value <= entry['max']:
                    raise ValueError(key+'采样不在现有行程内')
        a,b,c,opened,closed = [card['positions'] for card in cards]
        # The same geometric conventions as torque-off whole_calibration.py.
        for i, delta_deg, ref_deg in ((2,45,-math.degrees(O1)),(3,-45,math.degrees(O2))):
            key=f'{port}:{i}'; delta=b[key]-a[key]
            if not 285 <= abs(delta) <= 740 or abs(c[key]-b[key]) > 80:
                raise ValueError(key+'的A/B/C几何变化不符')
            result['joints'][key].update(reference_raw=a[key], reference_deg=ref_deg,
                direction=1 if delta*delta_deg > 0 else -1, verified=True,
                reference_evidence=[cards[j]['evidence'] for j in (0,1,2)])
        key=f'{port}:4'; delta=c[key]-b[key]
        if not 285 <= abs(delta) <= 740 or abs(a[key]-b[key]) > 80:
            raise ValueError(key+'腕俯仰A/B/C变化不符')
        result['joints'][key].update(reference_raw=b[key], reference_deg=0.,
            direction=-1 if delta > 0 else 1, verified=True,
            reference_evidence=[cards[j]['evidence'] for j in (0,1,2)])
        key=f'{port}:6'; low=closed[key]; high=opened[key]
        if abs(high-low) < 48:
            raise ValueError(key+'夹爪开合采样太近')
        direction=1 if high>low else -1
        result['joints'][key].update(open_raw=high-direction*8, closed_raw=low+direction*8,
            verified=True, reference_evidence=[cards[j]['evidence'] for j in (3,4)])
    result.update(kind='observed_powered_whole_robot_calibration', references_verified=True,
        references_captured_at=now(), method='powered_observed_geometric_ABC_no_register_write',
        powered_reference_captures=deepcopy(captures),
        accuracy_note='相机人工复核的几何对位，用于低速遥控初始化；不是测量级标定')
    return result


def read_captures(root, manifest):
    captures={}
    for name, record in manifest['cards'].items():
        if record.get('geometry_reviewed') is not True or not record.get('review_note'):
            raise ValueError(name+'缺少实际画面几何复核')
        rid=record['run_id']
        if not isinstance(rid,str) or not re.fullmatch(r'\d{8}-\d{6}',rid):
            raise ValueError('运行编号无效')
        directory=root/rid
        run=json.loads((directory/'result.json').read_text(encoding='utf-8'))
        if run.get('error'):
            raise ValueError(name+'来自失败运行')
        step=next((s for s in run['results'] if s['step']==record['step']),None)
        if not step or step.get('op')!='snapshot' or step.get('status')!='done':
            raise ValueError(name+'不是成功的观察步骤')
        images=[s for s in step.get('shots',[]) if s.get('file') and s.get('bytes',0)>0]
        if not images or any(not (directory/shot['file']).is_file() for shot in images):
            raise ValueError(name+'缺少原始图像')
        captures[name]={'port':record['port'],'positions':step['positions'],
            'evidence':{'run_id':rid,'step':record['step'],'at':step['at'],
                        'images':[shot['file'] for shot in images], 'review_note':record['review_note']}}
    return captures


def request(path, body=None):
    req=urllib.request.Request('http://127.0.0.1:8765'+path,
        data=None if body is None else json.dumps(body).encode('utf-8'),
        headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=30) as reply:
        return json.load(reply)


def restore_bytes(path, content):
    temporary=path.with_suffix('.restore.tmp')
    with temporary.open('wb') as stream:
        stream.write(content);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def assert_idle(state):
    if (state['controller']['controller']!='offline' or state['agent_run']['running']
            or state['limit_selftest']['running'] or state['manual_calibration']['active']):
        raise ValueError('接管、自检或采集正在进行，未写标定')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest',type=Path)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    manifest=json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    profile_path=Path('fabrication/assembly/kinematic-calibration.json')
    before=profile_path.read_bytes()
    if hashlib.sha256(before).hexdigest()!=manifest['profile_sha256']:
        raise ValueError('标定在复核后已变化；保留当前文件并重新核对')
    state=request('/api/original/status'); assert_idle(state)
    if state.get('preview_only'):
        raise ValueError('预览服务不能应用实体标定')
    motors={p['port']+':'+str(m['id']):m for p in state['ports'] for m in p['motors']}
    if len(motors)!=16 or any(not port.get('connected') for port in state['ports']):
        raise ValueError('16个电机未全部在线')
    captures=read_captures(Path(manifest['runs_root']),manifest)
    if manifest['arm_ports']!=state['manual_calibration']['arm_ports']:
        raise ValueError('双臂映射已变化')
    proposed=build_references(json.loads(before),captures,manifest['arm_ports'],motors)
    out=args.manifest.parent
    atomic_json(out/'proposed-profile.json',proposed)
    print(json.dumps({'proposal':str(out/'proposed-profile.json'),'references':8,'register_writes':0}))
    if not args.apply:return
    assert_idle(request('/api/original/status'))
    # Recheck bytes immediately before replacement; never overwrite new work.
    if profile_path.read_bytes()!=before:raise ValueError('标定并发更新，未覆盖')
    backup=out/'previous-profile.json'
    if backup.exists():raise ValueError('本次运行已有备份，请使用新的独立复核目录')
    backup.write_bytes(before)
    try:
        atomic_json(profile_path,proposed)
        request('/api/calibration/read',{})
        after=request('/api/original/status')
        setup=after['whole_robot_setup']
        if not setup['ready']:raise ValueError('应用后整机检查未通过：'+json.dumps(setup,ensure_ascii=False))
        for port in after['ports']:
            for motor in port['motors']:
                prior=motors[port['port']+':'+str(motor['id'])]
                if any(prior[k]!=motor[k] for k in ('min','max','homing_raw','mode','torque')):
                    raise ValueError('应用前后电机配置变化')
        atomic_json(out/'applied-result.json',{'at':now(),'setup':setup,'register_writes':0,
                    'profile_sha256':hashlib.sha256(profile_path.read_bytes()).hexdigest()})
        print(json.dumps({'applied':True,'setup':setup,'register_writes':0},ensure_ascii=False))
    except Exception:
        # Restore only our own result, never overwrite a later writer.
        if json.loads(profile_path.read_bytes())==proposed:
            restore_bytes(profile_path,before);request('/api/calibration/read',{})
        raise


if __name__=='__main__':main()
