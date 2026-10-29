"""Persisted, user-tuned whole-controller settings.

The arm mapping describes the logical controller side for each physical bus.
It lets a mechanically reversed installation behave as the upstream layout.
"""
from __future__ import annotations
import json,os
from pathlib import Path

DEFAULTS={
    'arm_mapping':'normal','joint_rate_deg_s':12.0,'grip_raw_s':120,
    'wheel_max_raw':800,'follow_delta_raw':96,'wheel_mapping':'normal'}
LIMITS={
    'joint_rate_deg_s':(1.0,30.0),'grip_raw_s':(20,400),
    'wheel_max_raw':(100,3000),'follow_delta_raw':(16,256)}


class ControllerSettings:
    def __init__(self,path):self.path=Path(path);self.data=self.load()

    def validate(self,data):
        data={**DEFAULTS,**(data or {})}
        if data.get('arm_mapping') not in ('normal','swapped'):raise ValueError('双臂映射必须为normal或swapped')
        if data.get('wheel_mapping') not in ('normal','swapped'):raise ValueError('左右轮映射必须为normal或swapped')
        for key,(low,high) in LIMITS.items():
            try:value=float(data[key])
            except (TypeError,ValueError):raise ValueError(f'{key}必须是数字') from None
            if not low<=value<=high:raise ValueError(f'{key}需在{low:g}–{high:g}')
            data[key]=int(value) if key.endswith('_raw') or key.endswith('_s') and key=='grip_raw_s' else value
        return data

    def load(self):
        try:return self.validate(json.loads(self.path.read_text(encoding='utf-8')))
        except (OSError,ValueError,TypeError,json.JSONDecodeError):return dict(DEFAULTS)

    def save(self,data):
        data=self.validate(data);self.path.parent.mkdir(parents=True,exist_ok=True)
        name=os.getpid();temporary=self.path.with_name(f'{self.path.name}.{name}.tmp')
        with temporary.open('w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(temporary,self.path);self.data=data;return self.snapshot()

    def snapshot(self):
        ports=self.arm_ports()
        return {**self.data,'logical_ports':ports,'logical_wheels':self.wheel_ids(),
                'description':'机械臂装反时选择swapped：手柄左杆控制物理左侧，换到另一块控制板'}

    def arm_ports(self):
        if self.data['arm_mapping']=='swapped':return {'left':'COM4','right':'COM3'}
        return {'left':'COM3','right':'COM4'}

    def side_for_port(self,port):
        return next((side for side,p in self.arm_ports().items() if p==port),port.lower())

    def wheel_ids(self):
        return {'left':10,'right':9} if self.data['wheel_mapping']=='swapped' else {'left':9,'right':10}


def remap_motor_labels(report,settings):
    """Change operator-facing arm labels without altering physical motor IDs."""
    for port in report.get('ports',[]):
        side=settings.side_for_port(port['port'])
        for motor in port.get('motors',[]):
            i=motor['id']
            if i<=6:motor['label']=('左臂' if side=='left' else '右臂')+['肩水平','肩抬升','肘','腕俯仰','腕旋转','夹爪'][i-1]
    return report
