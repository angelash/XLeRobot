"""Persisted, user-tuned whole-controller settings.

The arm mapping describes the logical controller side for each physical bus.
It lets a mechanically reversed installation behave as the upstream layout.
"""
from __future__ import annotations
import json,os
from pathlib import Path

DEFAULTS={
    'arm_mapping':'normal','joint_rate_deg_s':12.0,'grip_raw_s':120,
    'wheel_max_raw':800,'follow_delta_raw':96,'wheel_mapping':'normal',
    'base_forward':'a','base_turn':'normal','axis_overrides':{}}
# Direct trial is meant to reveal real behaviour, so the user-facing ceilings
# are generous. STS3215 goal velocity is additionally capped at 1000 raw/s.
LIMITS={
    'joint_rate_deg_s':(1.0,90.0),'grip_raw_s':(20,800),
    'wheel_max_raw':(100,3000),'follow_delta_raw':(16,512)}
# Field-observed chassis directions for a mirrored differential drive.
# base_forward flips only translation, base_turn flips only rotation, so a
# wrong forward test and a wrong turn test each have one independent fix.
MAPPING_KEYS=('arm_mapping','wheel_mapping','base_forward','base_turn')


def migrate_legacy(data):
    """Old files stored only wheel_mapping (forward default "a").

    Old (mapping, forward) produced motor commands equivalent to new
    (forward=f*m, turn=f). With forward "a", swapped == forward reversed.
    """
    data=dict(data or {})
    if 'base_turn' not in data:
        f=-1 if data.get('base_forward')=='reverse' else 1
        m=-1 if data.get('wheel_mapping')=='swapped' else 1
        data['base_forward']='a' if f*m==1 else 'reverse'
        data['base_turn']='normal' if f==1 else 'reverse'
        data['wheel_mapping']='normal'
    return data


class ControllerSettings:
    def __init__(self,path):self.path=Path(path);self.data=self.load()

    def validate(self,data):
        data={**DEFAULTS,**(data or {})}
        if data.get('arm_mapping') not in ('normal','swapped'):raise ValueError('双臂映射必须为normal或swapped')
        if data.get('wheel_mapping') not in ('normal','swapped'):raise ValueError('左右轮映射必须为normal或swapped')
        if data.get('base_forward') not in ('a','reverse'):raise ValueError('车身前进定义必须为a或reverse')
        if data.get('base_turn') not in ('normal','reverse'):raise ValueError('转向方向必须为normal或reverse')
        overrides=data.get('axis_overrides') or {}
        if not isinstance(overrides,dict):raise ValueError('逐轴设置必须是对象')
        allowed={'joint_rate_deg_s','direction','follow_delta_raw','grip_raw_s'}
        valid_keys={f'{port}:{i}' for port in ('COM3','COM4') for i in range(1,7)}|{'COM3:7','COM3:8'}
        checked={}
        for key,value in overrides.items():
            if key not in valid_keys or not isinstance(value,dict) or not set(value)<=allowed:raise ValueError('逐轴设置键无效')
            item={}
            if 'direction' in value:
                if value['direction'] not in (-1,1):raise ValueError('关节方向必须为±1')
                item['direction']=value['direction']
            for key_name,(low,high) in {'joint_rate_deg_s':LIMITS['joint_rate_deg_s'],'follow_delta_raw':LIMITS['follow_delta_raw'],'grip_raw_s':LIMITS['grip_raw_s']}.items():
                if key_name in value:
                    try:number=float(value[key_name])
                    except (TypeError,ValueError):raise ValueError('逐轴速度必须是数字') from None
                    if not low<=number<=high:raise ValueError(f'{key}的{key_name}需在{low:g}–{high:g}')
                    item[key_name]=int(number) if key_name!='joint_rate_deg_s' else number
            checked[key]=item
        data['axis_overrides']=checked
        for key,(low,high) in LIMITS.items():
            try:value=float(data[key])
            except (TypeError,ValueError):raise ValueError(f'{key}必须是数字') from None
            if not low<=value<=high:raise ValueError(f'{key}需在{low:g}–{high:g}')
            data[key]=int(value) if key.endswith('_raw') or key.endswith('_s') and key=='grip_raw_s' else value
        return data

    def load(self):
        try:return self.validate(migrate_legacy(json.loads(self.path.read_text(encoding='utf-8'))))
        except (OSError,ValueError,TypeError,json.JSONDecodeError):return dict(DEFAULTS)

    def save(self,data):
        data=self.validate(data);self.path.parent.mkdir(parents=True,exist_ok=True)
        name=os.getpid();temporary=self.path.with_name(f'{self.path.name}.{name}.tmp')
        with temporary.open('w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(temporary,self.path);self.data=data;return self.snapshot()

    def snapshot(self):
        ports=self.arm_ports()
        return {**self.data,'logical_ports':ports,'logical_wheels':self.wheel_ids(),'limits':LIMITS,
                'description':'机械臂装反时选择swapped：手柄左杆控制物理左侧，换到另一块控制板'}

    def base_signs(self):
        """(translation_sign, rotation_sign) applied before wheel kinematics."""
        return (-1 if self.data.get('base_forward')=='reverse' else 1,
                -1 if self.data.get('base_turn')=='reverse' else 1)

    def merged(self,patch):
        """Partial update: omitted fields keep their saved values."""
        data={**self.data,**{k:v for k,v in (patch or {}).items() if v is not None}}
        if 'axis_overrides' in (patch or {}) and patch['axis_overrides'] is not None:
            data['axis_overrides']=patch['axis_overrides']
        return data

    def arm_ports(self):
        if self.data['arm_mapping']=='swapped':return {'left':'COM4','right':'COM3'}
        return {'left':'COM3','right':'COM4'}

    def side_for_port(self,port):
        return next((side for side,p in self.arm_ports().items() if p==port),port.lower())

    def wheel_ids(self):
        return {'left':10,'right':9} if self.data['wheel_mapping']=='swapped' else {'left':9,'right':10}

    def axis(self,port,i):
        key=f'{port}:{i}';item=self.data.get('axis_overrides',{}).get(key,{})
        return {'direction':item.get('direction',1),
                'joint_rate_deg_s':item.get('joint_rate_deg_s',self.data['joint_rate_deg_s']),
                'grip_raw_s':item.get('grip_raw_s',self.data['grip_raw_s']),
                'follow_delta_raw':item.get('follow_delta_raw',self.data['follow_delta_raw'])}


def remap_motor_labels(report,settings):
    """Change operator-facing arm labels without altering physical motor IDs."""
    for port in report.get('ports',[]):
        side=settings.side_for_port(port['port'])
        for motor in port.get('motors',[]):
            i=motor['id']
            if i<=6:motor['label']=('左臂' if side=='left' else '右臂')+['肩水平','肩抬升','肘','腕俯仰','腕旋转','夹爪'][i-1]
    return report
