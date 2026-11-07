"""Local recovery assessment using the existing controller's read-only HTTP API.

Produces evidence and the next recovery step, never motor commands, automatic
hold removal, serial connections, or a claim that a scene is physically clear.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import time
import urllib.request
import uuid

EXPECTED = {'COM3': tuple(range(1, 9)), 'COM4': (1, 2, 3, 4, 5, 6, 9, 10)}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def wheel_observation(state):
    try:
        at = datetime.fromisoformat(state['updated_at']).timestamp()
        wheels = {m['id']:m for p in state['ports'] if p['port']=='COM4'
                  for m in p['motors'] if m['id'] in (9,10)}
        return {'at':at,'wheels':wheels}
    except (KeyError, TypeError, ValueError):
        return {'at':None,'wheels':{}}


def parked_wheels(rows, now=None):
    """Require an observed torque-off zero-goal window, never a single good read."""
    if not isinstance(rows,list) or len(rows)<3:
        return False
    try:
        times=[r['at'] for r in rows]
        if (not all(number(t) for t in times) or any(b<=a for a,b in zip(times,times[1:]))
                or not .6<=times[-1]-times[0]<=2.5
                or (now is not None and not -.2<=now-times[-1]<=2.)):
            return False
        for i in (9,10):
            values=[r['wheels'][i] for r in rows]
            if not all(m['torque']==0 and m['goal_velocity']==0 and m['mode']==1
                       and m['status']==0 and type(m['speed']) is int
                       and number(m['voltage']) and 11.5<=m['voltage']<=12.6
                       and number(m['temperature']) and 0<m['temperature']<45
                       and 0<=m['speed']<=65535 and (m['speed']&0x7fff)<=50
                       and type(m['position']) is int and 0<=m['position']<=4095 for m in values):
                return False
            origin=values[0]['position']
            deltas=[(m['position']-origin+2048)%4096-2048 for m in values]
            if max(deltas)-min(deltas)>2:
                return False
        return True
    except (KeyError, TypeError):
        return False


def collect_parked_history(state, fetch_next, clock=time.monotonic, wait=time.sleep):
    rows=[];deadline=clock()+2.5
    while True:
        row=wheel_observation(state)
        if not number(row['at']):return state,rows
        if not rows or row['at']>rows[-1]['at']:rows.append(row)
        if (len(rows)>=3 and rows[-1]['at']-rows[0]['at']>=.6) or clock()>=deadline:
            return state,rows
        wait(.12);state=fetch_next()


def hardware_readiness(state, now=None, parked_history=None):
    """Exact topology and fresh telemetry; an empty list is never ready."""
    now = time.time() if now is None else now
    issues, motors = [], {}
    try:
        at = datetime.fromisoformat(state['updated_at']).timestamp()
        age = now-at
        if not -.2 <= age <= 2.:
            issues.append('stale_hardware_snapshot')
    except (KeyError, TypeError, ValueError, OverflowError):
        age = None
        issues.append('unknown_hardware_snapshot_time')
    if state.get('error'):
        issues.append('controller_read_error')
    for port in state.get('ports') or []:
        name = port.get('port')
        if name not in EXPECTED:
            issues.append('unexpected_port')
            continue
        if not port.get('connected') or port.get('error'):
            issues.append('port_not_ready:'+name)
        for motor in port.get('motors') or []:
            key = (name, motor.get('id'))
            if isinstance(key[1], bool) or key[1] not in EXPECTED[name]:
                issues.append('unexpected_motor:'+name)
                continue
            if key in motors:
                issues.append('duplicate_motor:'+name+':'+str(key[1]))
            motors[key] = motor
    missing = [f'{port}:{i}' for port, ids in EXPECTED.items() for i in ids if (port,i) not in motors]
    if missing:
        issues.append('missing_motor_readback')
    torque_off = []
    observation=wheel_observation(state)
    parked=(parked_wheels(parked_history,now) and parked_history[-1]==observation)
    for (port, i), motor in motors.items():
        name = f'{port}:{i}'
        wheel = port=='COM4' and i in (9, 10)
        for field in ('position','speed','goal_velocity','voltage','temperature','status','torque','mode'):
            if not number(motor.get(field)):
                issues.append('invalid_'+field+':'+name)
        if motor.get('model') != 777:
            issues.append('unexpected_model:'+name)
        if motor.get('mode') != (1 if wheel else 0):
            issues.append('unexpected_mode:'+name)
        if motor.get('status') != 0:
            issues.append('motor_status_error:'+name)
        voltage, temperature = motor.get('voltage'), motor.get('temperature')
        if not number(voltage) or not 11.5 <= voltage <= 12.6:
            issues.append('voltage_not_ready:'+name)
        if not number(temperature) or not 0 < temperature < 45:
            issues.append('temperature_not_ready:'+name)
        if motor.get('torque') != 1:
            torque_off.append(name)
        if wheel and not parked and (motor.get('speed') != 0 or motor.get('goal_velocity') != 0):
            issues.append('wheels_not_stopped:'+name)
    complete = len(motors)==16 and not missing
    all_torque = complete and not torque_off
    all_arm_torque=complete and not any(k not in ('COM4:9','COM4:10') for k in torque_off)
    return {'ready':not issues and (all_torque or (parked and all_arm_torque)), 'telemetry_ready':not issues,
            'online_confirmed':len(motors), 'expected':16, 'missing':missing,
            'all_torque':all_torque, 'torque_off':torque_off, 'snapshot_age_s':age,
            'all_arm_torque':all_arm_torque,'stationary_base_verified':bool(parked),
            'issues':list(dict.fromkeys(issues)),
            'scope':'observed readback only; absent records do not identify a power fault'}


def hold_kind(value):
    if not value:
        return None
    if isinstance(value, dict):
        kind = value.get('kind')
        return 'scene' if kind in ('scene','scene_replan') else 'physical'
    # Only the exact known class of historical scene hold is reclassified.
    # Unknown legacy reasons continue to require physical inspection.
    text = str(value).lower()
    if ('objects adjacent' in text and 'physical separation' in text
            and not any(s in text for s in ('contact','timeout','fault','电机','受阻'))):
        return 'scene'
    return 'physical'


def assess(state, *, side, holds=None, servo=None, cameras=None, now=None, parked_history=None):
    if side not in ('left','right'):
        raise ValueError('Unknown arm')
    hardware = hardware_readiness(state, now, parked_history)
    controller = state.get('controller') or {}
    busy = (bool((servo or {}).get('active')) or bool((servo or {}).get('busy'))
            or bool((state.get('agent_run') or {}).get('running'))
            or bool((state.get('limit_selftest') or {}).get('running'))
            or controller.get('controller') not in (None,'offline'))
    hold = (holds or {}).get(side)
    kind = hold_kind(hold)
    scene_path = ['observe_from_head_and_both_wrists','move_idle_arm_out_of_swept_volume',
                  'relocalize_at_clear_high_pose','plan_supported_separation_or_regrasp',
                  'if_needed_fold_arms_and_plan_bounded_base_reposition',
                  'verify_new_scene_before_new_frozen_epoch']
    if busy:
        next_step, reason = 'wait_for_current_operator', 'Another operation or operator owns the robot'
    elif not hardware['telemetry_ready']:
        if hardware['missing'] or 'controller_read_error' in hardware['issues']:
            next_step, reason = 'restore_motor_communication', 'Fresh complete motor readback is required; USB enumeration alone is insufficient'
        elif any('snapshot' in issue for issue in hardware['issues']):
            next_step, reason = 'refresh_hardware_observation', 'The motor inventory is not from a current timestamp'
        else:
            next_step, reason = 'inspect_hardware_fault', 'Inspect voltage, temperature, modes and status without rewriting configuration'
    elif not hardware['ready']:
        next_step, reason = 'prepare_hold_at_current_position', 'Lock fresh current poses with zero speed before enabling torque'
    elif any(not (cameras or {}).get(c,{}).get('fresh') for c in (0,1,2)):
        next_step, reason = 'restore_shared_camera_observation', 'All three current shared camera views are required'
    elif kind == 'physical':
        next_step, reason = 'diagnose_arm_before_reuse', 'Physical failure hold persists; no repeated grasp until the cause is resolved'
    else:
        next_step, reason = 'recover_scene_autonomously', 'Scene layout is a recovery task; fresh evidence replaces a manual-reset prerequisite'
    return {'side':side,'hardware':hardware,'next_step':next_step,'reason':reason,
            'hold':hold,'hold_kind':kind,'manual_scene_reset_required':False,
            'scene_recovery_steps':scene_path,'motion_enabled':False,'hold_cleared':False,
            'physical_recovery_verified':False,'source':'local_readonly_recovery_assessment'}


def fetch(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    output = root/'recovery-assessments'/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
    output.mkdir(parents=True, exist_ok=False)
    state, servo, errors = {}, {}, {}
    for name, endpoint in (('state','/api/status'),('servo','/api/servo/status')):
        try:
            value=fetch('http://127.0.0.1:8765'+endpoint)
            if name=='state':state=value
            else:servo=value
        except Exception as exc:
            errors[name]=type(exc).__name__+': '+str(exc)
    park_history=[]
    wheels=wheel_observation(state)['wheels']
    if len(wheels)==2 and all(m.get('torque')==0 for m in wheels.values()):
        try:
            state,park_history=collect_parked_history(state,lambda:fetch('http://127.0.0.1:8765/api/status'))
        except Exception as exc:errors['parked_history']=str(exc)
    cameras={}
    for camera in (0,1,2):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:8765/api/camera/{camera}/snapshot',timeout=3) as r:
                raw=r.read();at=float(r.headers['X-Captured-At']);seq=int(r.headers['X-Frame-Sequence'])
            age=time.time()-at
            (output/f'cam{camera}.jpg').write_bytes(raw)
            cameras[camera]={'fresh':-.1<=age<=.8,'captured_at':at,'sequence':seq,'age_s':age,'bytes':len(raw)}
        except Exception as exc:
            cameras[camera]={'fresh':False,'error':type(exc).__name__+': '+str(exc)}
    holds={}
    if (root/'ARM_HOLDS.json').exists():
        try:
            holds=json.loads((root/'ARM_HOLDS.json').read_text(encoding='utf8'))
            if not isinstance(holds,dict):raise ValueError('Expected hold object')
        except Exception as exc:
            errors['holds']=str(exc)
            holds={s:{'kind':'physical','reason':'Unreadable hold ledger'} for s in ('left','right')}
    report={'at':datetime.now().astimezone().isoformat(),'state':state,'servo':servo,
            'wheel_park_history':park_history,
            'cameras':cameras,'holds':holds,'errors':errors,'motor_commands':0,
            'arms':{s:assess(state,side=s,holds=holds,servo=servo,cameras=cameras,parked_history=park_history) for s in ('left','right')}}
    (output/'assessment.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'output':str(output),'motor_commands':0,
                      'arms':{s:{k:r[k] for k in ('next_step','hold_kind','hardware','manual_scene_reset_required')} for s,r in report['arms'].items()}}))


if __name__=='__main__':
    main()
