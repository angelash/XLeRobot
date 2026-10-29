"""Observed, torque-off commissioning for the upstream whole-robot mapping.

No synthetic reference pose or range is promoted to a physical measurement.
All I/O is performed inside OriginalRuntime.io, using its existing bus owner.
"""
from __future__ import annotations
from collections import deque
from copy import deepcopy
from dataclasses import dataclass,field
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .commissioning import configure_measured_range,restore_test_window
from .control_runtime import EXPECTED_SERIALS,joint_label,now
from .torque_control import EXPECTED,read_registers,word,write_register
from .upstream_teleop import O1,O2,encode_velocity

KEYS=tuple(f'{p}:{i}' for p,ids in EXPECTED.items() for i in ids if not(p=='COM4' and i in (9,10)))
STEPS=('ranges','left_a','left_b','left_c','left_open','left_closed',
       'right_a','right_b','right_c','right_open','right_closed','base','review','complete')
DESIGN_GEOMETRY={'radius_m':.0635,'track_m':.25,'geometry_source':'published_design',
 'sources':['docs/en/source/hardware/getting_started/assemble_2wheel.md: 5 inch wheel',
            'software/src/robots/xlerobot_2wheels/config_xlerobot_2wheels.py: wheelbase=0.25'],
 'note':'轮径127mm采用0.4装配页；轮距250mm采用双轮控制配置；未做实机尺寸测量'}


def atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as f:
        json.dump(data,f,ensure_ascii=False,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)


class IndependentResult(dict):
    """Valid in-memory capture plus warnings that must remain visible."""
    def __init__(self,value,warnings=None):super().__init__(value);self.warnings=list(warnings or [])


def signed_homing(raw):
    return -(raw&0x7ff) if raw&0x800 else raw


def encode_homing(value):
    if not -2047<=value<=2047:raise ValueError('偏移超出STS3215编码范围')
    return abs(value)|(0x800 if value<0 else 0)


@dataclass
class RangeSample:
    raw:int
    at:float
    current:int=field(init=False)
    low:int=field(init=False)
    high:int=field(init=False)
    count:int=0
    recent:deque=field(default_factory=lambda:deque(maxlen=4))

    def __post_init__(self):
        if not 0<=self.raw<=4095:raise ValueError('位置不是12位编码器读数')
        self.current=self.low=self.high=self.raw
        self.recent.append((self.at,self.current))

    def update(self,raw,at):
        if not 0<=raw<=4095:raise ValueError('位置不是12位编码器读数')
        elapsed=at-self.at
        if elapsed<0 or elapsed>2:raise ValueError('采样间隔超过2秒，需重新采集，不能猜测跨零')
        if elapsed==0:
            if raw!=self.raw:raise ValueError('同一采样时刻读数改变，无法判断移动速度')
            return
        delta=(raw-self.raw+2048)%4096-2048
        if abs(delta)>min(1024,4096*elapsed+32):raise ValueError('单次变化过大，暂停采集；有效行程已保留，可恢复后补采')
        current=self.current+delta;low=min(self.low,current);high=max(self.high,current)
        if high-low>3800:raise ValueError('观察范围接近一整圈，请检查线束和采集方式后恢复补采')
        self.current=current;self.low=low;self.high=high
        self.raw=raw;self.at=at;self.count+=1;self.recent.append((at,self.current))

    def stationary(self,at):
        return (len(self.recent)>=3 and at-self.at<.6 and self.at-self.recent[0][0]<1.5
                and max(x[1] for x in self.recent)-min(x[1] for x in self.recent)<=5)

    def snapshot(self):
        return {'position':self.raw,'unwrapped':self.current,'min':self.low,'max':self.high,
                'span_raw':self.high-self.low,'span_deg':round((self.high-self.low)*360/4096,1),'samples':self.count}


def recentered(sample,old_homing,margin=8):
    """p=encoder-homing modulo 4096; center the *observed* unwrapped arc."""
    if sample.high-sample.low<48:raise ValueError('实测行程不足48计数，不能作为完整操作行程')
    mid=round((sample.low+sample.high)/2)
    offset=(signed_homing(old_homing)+mid-2047+2048)%4096-2048
    if offset==-2048:mid+=1;offset=-2047
    convert=lambda value:value-mid+2047
    # Keep the approved safety inset where there is room, but never create a
    # smaller range than the physically observed current position. This lets a
    # user save while resting at an endpoint and then command only inward.
    low=convert(sample.low);high=convert(sample.high);position=convert(sample.current)
    low_margin=margin if position-low>=margin else 0
    high_margin=margin if high-position>=margin else 0
    return {'min':low+low_margin,'max':high-high_margin,
            'homing_raw':encode_homing(offset),'position':position,'mid':mid}


def build_profile(samples,captures,backup,wheels,confirmed_keys,arm_ports=None):
    if set(confirmed_keys)!=set(KEYS):raise ValueError('需要逐项确认14个关节均已走过完整可用行程')
    needed=set(STEPS[1:11])
    if not needed<=set(captures):raise ValueError('双臂A/B/C与夹爪端点尚未完整采集')
    # Arm calibration and chassis validation are independent deliverables.
    # build_profile records the base state without requiring it.
    data={'schema':3,'kind':'observed_manual_whole_robot_calibration','serials':EXPECTED_SERIALS,
          'full_manual_range_verified':True,'captured_at':now(),'wheels':deepcopy(wheels),'joints':{},
          'method':'torque_off_unwrapped_range_and_geometric_ABC','raw_captures':deepcopy(captures)}
    for key in KEYS:
        sample=samples[key];entry=recentered(sample,word(backup[key],31))
        if not entry['min']<=entry['position']<=entry['max']:raise RuntimeError('内部范围换算错误')
        entry.update(observed_range=sample.snapshot(),full_range_confirmed=True,verified=True)
        data['joints'][key]=entry
    # Cards carry the physical bus on which they were recorded. It wins over a
    # later logical side mapping so a reversed installation cannot relabel data.
    arm_ports=arm_ports or {'left':'COM3','right':'COM4'}
    for side in ('left','right'):
        port=captures[side+'_a'].get('port') or arm_ports[side]
        if port not in EXPECTED:raise ValueError(side+'姿态缺少物理控制板绑定')
        a,b,c=[captures[side+'_'+pose]['positions'] for pose in ('a','b','c')]
        # A: upper link vertical, forearm horizontal. B: upper link 45deg
        # outward, forearm horizontal. C: keep B links, tilt tool upward 45deg.
        for i,delta_deg,ref_deg in [(2,45,-math.degrees(O1)),(3,-45,math.degrees(O2))]:
            key=f'{port}:{i}';delta=b[key]-a[key]
            if not 285<=abs(delta)<=740:raise ValueError(joint_label(port,i)+'的A→B应约45°，请重采参考姿态')
            if abs(c[key]-b[key])>80:raise ValueError('姿态C请保持上臂和前臂不动，只抬夹爪尖端')
            entry=data['joints'][key]
            entry.update(reference_raw=a[key]-entry['mid']+2047,reference_deg=ref_deg,
                         direction=1 if delta*delta_deg>0 else -1)
        key=f'{port}:4';delta=c[key]-b[key]
        if not 285<=abs(delta)<=740 or abs(a[key]-b[key])>80:
            raise ValueError(side+'腕俯仰参考变化不符，请按A/B/C示意重采')
        entry=data['joints'][key]
        entry.update(reference_raw=b[key]-entry['mid']+2047,reference_deg=0.,direction=-1 if delta>0 else 1)
        entry=data['joints'][f'{port}:6']
        opened=captures[side+'_open']['positions'][f'{port}:6']-entry['mid']+2047
        closed=captures[side+'_closed']['positions'][f'{port}:6']-entry['mid']+2047
        direction=1 if opened>closed else -1
        entry.update(open_raw=opened-direction*8,closed_raw=closed+direction*8)
        if (abs(entry['open_raw']-entry['closed_raw'])<24 or
            not all(entry['min']<=v<=entry['max'] for v in (entry['open_raw'],entry['closed_raw']))):
            raise ValueError(side+'夹爪端点过近或不在实测范围内，请重采')
    # Relative channels need no absolute angle; still store a valid observed
    # local origin to keep the calibration contract uniform.
    for key,entry in data['joints'].items():
        if key.endswith(':6'):continue
        entry.setdefault('reference_raw',entry['position']);entry.setdefault('reference_deg',0.)
        entry.setdefault('direction',1)
    return data


def split_key(key):
    port,i=key.split(':');return port,int(i)


class WholeCalibration:
    def __init__(self,runtime,path,profile_path):
        self.runtime=runtime;self.path=Path(path);self.profile_path=Path(profile_path)
        self.active=False;self.step='ranges';self.error=None;self.id=None
        self.samples={};self.captures={};self.backup={};self.confirmed_keys=[]
        self.wheels={};self.base_tests={};self.base_running=None;self.timer=None
        self.last_save=0.;self.applied=False;self.busy=False
        self.recovery_reset=[];self.recovery_sources=[];self.recoverable=None;self.recovery_checked=0.
        self.persistence_error=None;self.step_warnings=[]

    def persist(self):
        try:atomic_json(self.path,{'id':self.id,'serials':EXPECTED_SERIALS,'updated_at':now(),
          'active':self.active,'step':self.step,'error':self.error,'applied':self.applied,
          'register_backup':self.backup,'ranges':{k:v.snapshot() for k,v in self.samples.items()},
          'captures':self.captures,'confirmed_keys':self.confirmed_keys,'wheels':self.wheels,
          'base_tests':self.base_tests,'recovery_reset':self.recovery_reset,'recovery_sources':self.recovery_sources})
        except OSError as exc:
            self.persistence_error=str(exc);return False
        self.persistence_error=None;self.last_save=time.monotonic();return True

    def start(self,confirmed):
        r=self.runtime
        if not confirmed:raise ValueError('请先托住双臂重量并确认开始手动标定')
        if self.active:raise ValueError('已有手动标定进行中')
        if r.controller['controller']=='online':raise ValueError('请先结束接管，再开始手动标定')
        r.scan();self.id=uuid.uuid4().hex;self.path=self.path.parent/(self.id+'.json')
        self.backup={f'{p}:{i}':read_registers(r.session.ports[p],i) for p,ids in EXPECTED.items() for i in ids}
        for key,reg in self.backup.items():
            wheel=key in ('COM4:9','COM4:10')
            if (word(reg,3)!=777 or reg[33]!=(1 if wheel else 0) or
                not 10<=reg[62]/10<=13 or reg[63]>=50 or reg[65]):
                raise ValueError(key+'的型号/模式/供电/温度/状态不满足标定')
        self.error=None;self.step='ranges';self.captures={};self.samples={};self.applied=False
        self.confirmed_keys=[];self.wheels={};self.base_tests={};self.base_running=None
        self.recovery_reset=[];self.recovery_sources=[]
        self.persist()  # Durable original register bytes precede every write.
        try:
            self.release_all()
            r.moving={};r.feedback_window={};r.direct_targets={};r.wheels_commanded=False;r.wheel_goals=[0,0]
            r.scan();stamp=time.monotonic()
            self.samples={f'{p["port"]}:{m["id"]}':RangeSample(m['position'],stamp)
                          for p in r.report['ports'] for m in p['motors'] if f'{p["port"]}:{m["id"]}' in KEYS}
            if set(self.samples)!=set(KEYS):raise ValueError('没有完整读到14个位置关节')
            self.active=True;self.persist()
            r.log('whole_calibration','已备份16颗寄存器并关闭扭矩，开始手动采集；未发送位置目标',id=self.id)
        except Exception as exc:
            self.error=str(exc)
            try:self.release_all()
            except Exception as cleanup:self.error+='；释放未确认：'+str(cleanup)
            self.active=False;self.persist();raise RuntimeError(self.error) from exc
        return self.snapshot()

    def resume_latest(self):
        """Continue the best torque-off checkpoint after a healthy restart."""
        if self.active:return self.snapshot()
        r=self.runtime
        if r.controller['controller']!='offline' or any(m['torque'] for p in r.report['ports'] for m in p['motors']):
            raise ValueError('恢复采集要求手柄离线且所有扭矩关闭')
        candidate=self.find_recoverable()
        if not candidate:raise ValueError('没有可恢复的采集记录')
        return self.recover(candidate['id'],True)

    def find_recoverable(self):
        """Find only this robot's non-applied checkpoints; never activate them."""
        stamp=time.monotonic()
        if stamp-self.recovery_checked<2:return self.recoverable
        candidates=[]
        for path in self.path.parent.glob('*.json'):
            if not re.fullmatch(r'[0-9a-f]{32}',path.stem):continue
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                if data.get('applied') or data.get('serials')!=EXPECTED_SERIALS:continue
                ranges=data['ranges']
                if set(ranges)!=set(KEYS):continue
                count=sum(v['span_raw']>=48 for v in ranges.values())
                candidates.append({'id':path.stem,'updated_at':data['updated_at'],
                                   'observed_joints':count,'total_span':sum(v['span_raw'] for v in ranges.values())})
            except (OSError,ValueError,KeyError,TypeError):continue
        self.recoverable=max(candidates,key=lambda c:(c['observed_joints'],c['total_span'],c['updated_at']),default=None)
        if self.recoverable and self.active and self.step=='cards' and not self.captures:
            self.recoverable['merge_checkpoint_id']=self._best_card_checkpoint()
        self.recovery_checked=stamp
        return self.recoverable

    def _best_card_checkpoint(self):
        best=None;best_count=0
        for path in self.path.parent.glob('*-before-recovery-*.json'):
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                captures=data.get('captures',{})
                if data.get('serials')!=EXPECTED_SERIALS or len(captures)<=best_count:continue
                best=path.stem;best_count=len(captures)
            except (OSError,ValueError,TypeError,json.JSONDecodeError):continue
        return best

    def recover(self,session_id,confirmed):
        """Re-anchor within an observed arc; reset only unresolvable joints.

        A checkpoint contains extrema, not a path across the interruption.
        No motion during that gap is inferred. Positions outside a recorded
        arc restart that joint's observation while other valid arcs survive.
        """
        r=self.runtime
        if not confirmed:raise ValueError('请确认双臂有支撑，恢复已保存的行程采样')
        if r.controller['controller']=='online' or self.base_running:raise ValueError('先结束接管或等待底盘停车')
        if self.active and not self.error:raise ValueError('当前采集正常，无需恢复')
        if not isinstance(session_id,str) or not re.fullmatch(r'[0-9a-f]{32}',session_id):raise ValueError('采集记录编号无效')
        path=self.path.parent/(session_id+'.json');data=json.loads(path.read_text(encoding='utf-8'))
        if data.get('serials')!=EXPECTED_SERIALS or data.get('applied') or data.get('id')!=session_id:
            raise ValueError('采集记录身份不符或已经应用')
        backup=data['register_backup'];ranges=data['ranges']
        if set(backup)!={f'{p}:{i}' for p,ids in EXPECTED.items() for i in ids} or set(ranges)!=set(KEYS):
            raise ValueError('采集记录不完整')
        # Read through the same owner. An old active error suppresses observing
        # this scan; healthy active collections were rejected above.
        r.scan();registers={}
        for key,before in backup.items():
            if len(before)!=70 or not all(type(v)is int and 0<=v<=255 for v in before):raise ValueError('寄存器备份格式不符')
            port,i=split_key(key);reg=read_registers(r.session.ports[port],i);registers[key]=reg
            if (word(reg,3)!=777 or reg[40]!=0 or reg[33]!=before[33] or reg[65] or
                not 10<=reg[62]/10<=13 or reg[63]>=50 or
                reg[:40]!=before[:40] or word(reg,48)!=word(before,48) or reg[55]!=before[55]):
                raise ValueError(joint_label(port,i)+'的扭矩/模式/限位/偏移/供电已变化，不能恢复旧行程')
            if key in ('COM4:9','COM4:10') and word(reg,46)!=0:raise ValueError('恢复采集前双轮目标速度必须为0')
        stamp=time.monotonic();samples={};reset=[]
        for key in KEYS:
            saved=ranges[key];low=saved['min'];high=saved['max'];raw=word(registers[key],56)
            if not(type(low)is int and type(high)is int and -8192<=low<=high<=12287 and high-low<=3800):
                raise ValueError(key+'保存的连续范围无效')
            candidates=[raw+4096*k for k in range(-3,4) if low<=raw+4096*k<=high]
            sample=RangeSample(raw,stamp)
            if len(candidates)==1:
                sample.low=low;sample.high=high;sample.current=candidates[0]
                sample.count=int(saved['samples']);sample.recent.clear();sample.recent.append((stamp,sample.current))
            else:reset.append({'key':key,'label':joint_label(*split_key(key))})
            samples[key]=sample
        # Keep the original checkpoint independently reviewable before replacing
        # its interrupted state. This does not change a motor register.
        atomic_json(path.with_name(session_id+'-before-recovery-'+uuid.uuid4().hex[:8]+'.json'),data)
        self.id=session_id;self.path=path;self.backup=backup;self.samples=samples
        self.captures=deepcopy(data.get('captures',{}));self.confirmed_keys=list(data.get('confirmed_keys',[]))
        self.wheels=deepcopy(data.get('wheels',{}));self.base_tests=deepcopy(data.get('base_tests',{}))
        # Historical cards used original physical naming (COM3 left, COM4
        # right). Preserve their saved port while deriving display-side names
        # from today's mapping.
        if self.captures:
            historical={'left':'COM3','right':'COM4'}
            for side in ('left','right'):
                for kind in ('a','b','c','open','closed'):
                    item=side+'_'+kind
                    if item not in self.captures:continue
                    port=self.captures[item].get('port') or historical[side]
                    if port not in EXPECTED:port=historical[side]
                    self.captures[item]={**self.captures[item],'port':port}
        self.step='cards'
        self.error=None;self.active=True;self.applied=False;self.recovery_reset=reset
        self.recovery_sources=[session_id];self.recoverable=None;self.persist()
        r.log('calibration_recovered','已恢复可确认的实测行程；仅补采当前位置超出旧记录的关节',
              id=session_id,reset=[x['key'] for x in reset])
        return self.snapshot()

    def merge_checkpoint(self,session_id,confirmed):
        """Merge independent cards from a previously verified checkpoint."""
        self.require_active()
        if not confirmed:raise ValueError('请确认合并已保存的采集卡片')
        if not isinstance(session_id,str) or not re.fullmatch(r'[0-9a-f]{32}-before-recovery-[0-9a-f]{8}',session_id):
            raise ValueError('备份记录编号无效')
        data=json.loads((self.path.parent/(session_id+'.json')).read_text(encoding='utf-8'))
        if data.get('serials')!=EXPECTED_SERIALS:raise ValueError('备份不属于当前机器人')
        ports=self.runtime.arm_ports();valid={'left_a','left_b','left_c','left_open','left_closed',
            'right_a','right_b','right_c','right_open','right_closed'}
        merged=[]
        for item,value in data.get('captures',{}).items():
            if item not in valid or 'positions' not in value:continue
            side=item.split('_',1)[0];port=value.get('port') or ports[side]
            missing=[key for key in self.samples if key not in value['positions']]
            if missing:continue
            self.captures[item]={**value,'port':port};merged.append(item)
        self.confirmed_keys=list(dict.fromkeys([*self.confirmed_keys,*data.get('confirmed_keys',[])]))
        self.step='cards';ok=self.persist()
        warnings=['历史备份未能写入磁盘：'+self.persistence_error] if not ok else []
        return IndependentResult(self.snapshot(),warnings)

    def release_all(self):
        failures=[]
        for p,ids in EXPECTED.items():
            for i in ids:
                try:
                    if p=='COM4' and i in (9,10):write_register(self.runtime.session.ports[p],i,46,0,2)
                    write_register(self.runtime.session.ports[p],i,40,0,1)
                    reg=read_registers(self.runtime.session.ports[p],i)
                    if reg[40]!=0 or (p=='COM4' and i in (9,10) and word(reg,46)!=0):
                        raise RuntimeError('关扭矩/轮速0回读不符')
                except Exception as exc:failures.append(f'{p}:{i} {exc}')
        if failures:raise RuntimeError('；'.join(failures))

    def observe(self,report):
        if not self.active or self.error or self.busy:return
        try:
            stamp=time.monotonic()
            motors={f'{p["port"]}:{m["id"]}':m for p in report['ports'] for m in p['motors']}
            for key,sample in self.samples.items():
                m=motors[key];before=self.backup[key]
                if m['torque']!=0 or m['mode']!=0:raise ValueError(m['label']+'采集期间扭矩/模式改变')
                if any(m[k]!=v for k,v in [('min',word(before,9)),('max',word(before,11)),('homing_raw',word(before,31))]):
                    raise ValueError(m['label']+'采集期间坐标参数改变')
                try:sample.update(m['position'],stamp)
                except ValueError as exc:raise ValueError(m['label']+'：'+str(exc)) from exc
            if stamp-self.last_save>2:self.persist()
        except Exception as exc:self.fail(str(exc))

    def fail(self,message):
        if self.active:
            self.error=message
            try:self.release_all()
            except Exception as exc:self.error+='；关闭未确认：'+str(exc)
            self.persist()

    def require_active(self):
        if not self.active:raise ValueError('先开始手动标定')
        if self.error:raise ValueError(self.error+'；请使用恢复采集保留有效行程')
        if self.base_running:raise ValueError('等待底盘短脉冲测试停车')

    def capture(self,step,confirmed_keys=None):
        self.require_active()
        if step not in STEPS or step!=self.step or step not in STEPS[:11]:raise ValueError('采集步骤与页面不一致')
        self.runtime.scan();self.require_active()
        if step=='ranges':
            keys=list(confirmed_keys or [])
            if set(keys)!=set(KEYS):raise ValueError('请逐项确认14个关节都已缓慢走过完整可用行程')
            for key,sample in self.samples.items():
                if sample.high-sample.low<48:raise ValueError(joint_label(*split_key(key))+'尚未采到有效行程')
            self.confirmed_keys=keys
        else:
            port='COM3' if step.startswith('left_') else 'COM4'
            ids=(6,) if step.endswith(('_open','_closed')) else (2,3,4)
            if not all(self.samples[f'{port}:{i}'].stationary(time.monotonic()) for i in ids):
                raise ValueError('请保持姿态不动约1秒，等待编码器稳定后再采集')
            positions={key:sample.current for key,sample in self.samples.items()}
            # Reject bad A/B/C immediately, before asking for subsequent poses.
            if step.endswith('_b'):
                a=self.captures[step[:-1]+'a']['positions']
                for i in (2,3):
                    if not 285<=abs(positions[f'{port}:{i}']-a[f'{port}:{i}'])<=740:
                        raise ValueError('A→B的肩和肘都应改变约45°，请对照示意调整')
                if abs(positions[f'{port}:4']-a[f'{port}:4'])>80:raise ValueError('A→B保持夹爪水平，腕俯仰相对前臂不变')
            if step.endswith('_c'):
                b=self.captures[step[:-1]+'b']['positions']
                if (not 285<=abs(positions[f'{port}:4']-b[f'{port}:4'])<=740 or
                    any(abs(positions[f'{port}:{i}']-b[f'{port}:{i}'])>80 for i in (2,3))):
                    raise ValueError('C只抬夹爪尖端约45°；上臂和前臂保持B姿态')
            self.captures[step]={'captured_at':now(),'positions':positions}
        self.step=STEPS[STEPS.index(step)+1];self.persist()
        return self.snapshot()

    def remap_captures(self,old_ports,new_ports):
        for item in list(self.captures):
            if '_' not in item:continue
            side,kind=item.split('_',1);port=self.captures[item].get('port') or old_ports.get(side)
            if not port:continue
            new_by_port={p:s for s,p in new_ports.items()}
            target=new_by_port.get(port,side)
            if target!=side:self.captures[target+'_'+kind]=self.captures.pop(item)
        self.persist()

    def capture_item(self,item,confirmed=False):
        self.require_active()
        if not confirmed:raise ValueError('请确认该项已摆稳且双臂有支撑')
        if item=='range':
            short=[key for key in KEYS if self.samples[key].high-self.samples[key].low<48]
            self.confirmed_keys=[key for key in KEYS if key not in short]
            self.step_warnings=[joint_label(*split_key(k))+'尚未采到有效行程' for k in short]
            self.persist();return IndependentResult(self.snapshot(),self.step_warnings)
        valid={'left_a','left_b','left_c','left_open','left_closed','right_a','right_b','right_c','right_open','right_closed'}
        if item not in valid:raise ValueError('未知采集卡片')
        self.runtime.scan();self.require_active()
        side,kind=item.split('_',1);port=self.runtime.arm_ports()[side]
        required=(6,) if kind in ('open','closed') else (2,3,4)
        if not all(self.samples[f'{port}:{i}'].stationary(time.monotonic()) for i in required):
            raise ValueError('请保持该项姿态约1秒，编码器稳定后重录')
        positions={key:s.current for key,s in self.samples.items()}
        self.captures[item]={'captured_at':now(),'port':port,'positions':positions}
        warnings=[]
        if kind=='b' and side+'_a' in self.captures:
            a=self.captures[side+'_a']['positions']
            if any(not 160<=abs(positions[f'{port}:{i}']-a[f'{port}:{i}'])<=900 for i in (2,3)):warnings.append('B与A的肩/肘变化不典型，已保存，可重录')
        if kind=='c' and side+'_b' in self.captures:
            b=self.captures[side+'_b']['positions']
            if abs(positions[f'{port}:4']-b[f'{port}:4'])<160:warnings.append('C的腕俯仰变化不明显，已保存，可重录')
        if kind in ('open','closed'):
            other_kind='closed' if kind=='open' else 'open';other=side+'_'+other_kind
            if other in self.captures and abs(positions[f'{port}:6']-self.captures[other]['positions'][f'{port}:6'])<24:warnings.append('夹爪两端差不明显，已保存，可重录')
        self.step_warnings=warnings;persisted=self.persist()
        if not persisted:warnings.append('磁盘保存失败，当前运行进程仍保留；请处理后刷新')
        return IndependentResult(self.snapshot(),warnings)

    def delete_capture(self,item):
        self.require_active()
        if item not in self.captures:raise ValueError('该项尚未录制')
        del self.captures[item];self.step_warnings=[];self.persist();return self.snapshot()

    def clear_captures(self,confirmed):
        """Start a fresh pose pass without deleting ranges or register backup."""
        self.require_active()
        if not confirmed:raise ValueError('请确认清除旧姿态引用并保留行程备份')
        if self.captures:
            atomic_json(self.path.with_name(self.id+'-captures-before-rerecord-'+uuid.uuid4().hex[:8]+'.json'),
                        {'id':self.id,'serials':EXPECTED_SERIALS,'captures':deepcopy(self.captures),'cleared_at':now()})
        self.captures={};self.step='cards';self.step_warnings=[];self.persist();return self.snapshot()

    def set_wheels(self,left_sign,right_sign,verified,confirmed):
        self.require_active()
        if left_sign not in (-1,1) or right_sign not in (-1,1) or not confirmed:
            raise ValueError('请选择双轮方向并确认现场已观察')
        self.wheels={**DESIGN_GEOMETRY,'left_sign':left_sign,'right_sign':right_sign,
                     'verified':bool(verified),'direction_observed':bool(verified),
                     'stop_verified':bool(verified),'confirmed_at':now() if verified else None}
        if not verified:self.wheels['note']=DESIGN_GEOMETRY['note']+'；底盘暂未开放，可稍后设置'
        self.persist();return self.snapshot()

    def rewind(self,step):
        self.require_active()
        if step not in STEPS[:11] or STEPS.index(step)>=STEPS.index(self.step):raise ValueError('只能返回已经采集过的步骤')
        self.captures={k:v for k,v in self.captures.items() if STEPS.index(k)<STEPS.index(step)}
        if step=='ranges':self.confirmed_keys=[]
        self.step=step;self.persist();return self.snapshot()

    def base_test(self,kind,left_sign,right_sign,confirmed):
        self.require_active()
        if kind not in ('forward','left') or not confirmed:
            raise ValueError('请勾选“双臂已放稳、车轮周围有空位”，再测试底盘')
        if left_sign not in (-1,1) or right_sign not in (-1,1):raise ValueError('轮子方向必须为±1')
        if self.wheels.get('left_sign')!=left_sign or self.wheels.get('right_sign')!=right_sign:self.base_tests={}
        self.wheels={**DESIGN_GEOMETRY,'left_sign':left_sign,'right_sign':right_sign,'verified':False}
        p=self.runtime.session.ports['COM4'];before={}
        try:
            self.runtime.scan();self.require_active()
            for i in (9,10):
                reg=read_registers(p,i)
                if reg[33]!=1 or reg[40]!=0 or not 10<=reg[62]/10<=13 or reg[63]>=50:raise ValueError('轮子速度模式/释放/供电/温度不满足短测')
                before[i]=word(reg,56);write_register(p,i,46,0,2)
            self.base_running=kind
            # Independent expiry thread is started before any nonzero velocity.
            self.timer=threading.Timer(.18,self._finish_base,args=(kind,before));self.timer.daemon=True;self.timer.start()
            signs=[left_sign,right_sign] if kind=='forward' else [-left_sign,right_sign]
            for i,sign in zip((9,10),signs):
                write_register(p,i,40,1,1);write_register(p,i,46,encode_velocity(160*sign),2)
        except Exception:
            if self.timer:self.timer.cancel()
            self.base_running=None;self.release_all();raise
        return self.snapshot()

    def _chassis_failure(self,message):
        self.base_running=None
        self.base_tests={}
        self.wheels={**DESIGN_GEOMETRY,'left_sign':self.wheels.get('left_sign',-1),
                     'right_sign':self.wheels.get('right_sign',1),'verified':False,
                     'direction_observed':False,'stop_verified':False,
                     'note':'底盘短测未通过，底盘暂未开放；'+message}
        self.step_warnings=['底盘短测未通过，双臂采集保留：'+message]
        self.persist()

    def _finish_base(self,kind,before):
        with self.runtime.io:
            if not self.active or self.base_running!=kind:return
            try:
                p=self.runtime.session.ports['COM4'];failures=[];results=[]
                for i in (9,10):
                    # Try every stop even if its peer has a lost ACK.
                    try:write_register(p,i,46,0,2)
                    except Exception as exc:failures.append(str(exc))
                    try:write_register(p,i,40,0,1)
                    except Exception as exc:failures.append(str(exc))
                if failures:raise RuntimeError('；'.join(failures))
                time.sleep(.05)
                for i in (9,10):
                    reg=read_registers(p,i);delta=(word(reg,56)-before[i]+2048)%4096-2048
                    if word(reg,46)!=0 or reg[40]!=0 or (word(reg,58)&0x7fff)>3:raise RuntimeError('轮子停车回读不符')
                    if abs(delta)<2:raise RuntimeError('轮子编码器未记录到有效位移')
                    results.append({'id':i,'delta':delta,'goal_velocity':0,'speed':word(reg,58),'torque':0})
                self.base_tests[kind]={'at':now(),'results':results};self.persist()
            except Exception as exc:
                self._chassis_failure(str(exc))
            finally:self.base_running=None;self.timer=None

    def confirm_base(self,forward_observed,left_observed):
        self.require_active()
        if self.step!='base' or not all(k in self.base_tests for k in ('forward','left')):
            raise ValueError('请先分别完成前进和左转短测')
        if not forward_observed or not left_observed:raise ValueError('请按现场真实方向确认；若反向先修改方向并重新测试')
        self.wheels.update(verified=True,direction_observed=True,stop_verified=True,
                           tests=deepcopy(self.base_tests),confirmed_at=now())
        self.step='review';self.persist();return self.snapshot()

    def apply(self,confirmed):
        self.require_active()
        if not confirmed:raise ValueError('请确认保存当前已采集的实测标定')
        r=self.runtime;r.scan();self.require_active()
        if not all(s.stationary(time.monotonic()) for s in self.samples.values()):raise ValueError('请让全部关节静止约1秒再保存')
        required={side+'_'+kind for side in ('left','right') for kind in ('a','b','c','open','closed')}
        missing=sorted(required-set(self.captures))
        if missing:raise ValueError('还缺少采集卡片：'+', '.join(missing)+'；底盘可稍后设置')
        wheels=deepcopy(self.wheels)
        if not wheels.get('verified'):
            wheels={**DESIGN_GEOMETRY,'verified':False,'direction_observed':False,
                    'stop_verified':False,
                    'note':DESIGN_GEOMETRY['note']+'；底盘可稍后设置方向与停车再开放'}
        profile=build_profile(self.samples,self.captures,self.backup,wheels,self.confirmed_keys,self.runtime.arm_ports())
        # Backup the previous portable file before changing any device bytes.
        old_profile=self.profile_path.read_text(encoding='utf-8') if self.profile_path.exists() else None
        atomic_json(self.path.with_name(self.id+'-apply-backup.json'),
                    {'registers':self.backup,'previous_profile':old_profile,'proposed':profile})
        changed=[];self.busy=True
        try:
            for key in KEYS:
                port,i=split_key(key);entry=profile['joints'][key];changed.append(key)
                before=read_registers(r.session.ports[port],i)
                if before[40]!=0 or abs((word(before,56)-self.samples[key].raw+2048)%4096-2048)>5:
                    raise ValueError(joint_label(port,i)+'保存期间移动或扭矩发生变化')
                configure_measured_range(r.session.ports[port],i,self.backup[key],entry['min'],entry['max'],entry['homing_raw'],entry['position'])
            # Verify the entire transaction, including unchanged wheels.
            for key in KEYS:
                port,i=split_key(key);reg=read_registers(r.session.ports[port],i);entry=profile['joints'][key]
                if reg[40]!=0 or any(word(reg,a)!=entry[k] for a,k in [(9,'min'),(11,'max'),(31,'homing_raw')]):
                    raise RuntimeError(key+'整机标定最终回读失败')
            self.release_all();atomic_json(self.profile_path,profile)
            self.applied=True;self.active=False;self.step='complete';self.persist()
            r.capture_profile();r.log('whole_calibration','完整行程、双臂参考、夹爪与底盘标定已保存；扭矩仍关闭',id=self.id)
        except Exception as exc:
            failures=[]
            for key in reversed(changed):
                port,i=split_key(key)
                try:restore_test_window(r.session.ports[port],i,self.backup[key])
                except Exception as cleanup:failures.append(key+' '+str(cleanup))
            if old_profile is not None:
                try:atomic_json(self.profile_path,json.loads(old_profile))
                except Exception as cleanup:failures.append('恢复旧标定文件 '+str(cleanup))
            elif self.profile_path.exists():
                try:self.profile_path.unlink()
                except OSError as cleanup:failures.append(str(cleanup))
            self.active=True;self.applied=False
            self.error=str(exc)+('；回滚未确认：'+'；'.join(failures) if failures else '；已回滚，保持扭矩关闭')
            try:self.release_all()
            except Exception as cleanup:self.error+='；释放未确认：'+str(cleanup)
            self.persist();raise RuntimeError(self.error) from exc
        finally:self.busy=False
        return self.snapshot()

    def cancel(self):
        if self.timer:self.timer.cancel();self.timer=None
        if self.active:self.release_all()
        self.active=False;self.base_running=None;self.persist()
        return self.snapshot()

    def snapshot(self):
        required={side+'_'+kind for side in ('left','right') for kind in ('a','b','c','open','closed')}
        labels={'a':'姿态 A · 上臂竖直','b':'姿态 B · 上臂外倾45°','c':'姿态 C · 夹爪抬高',
                'open':'夹爪全开','closed':'夹爪闭合'}
        missing_cards=[{'item':item,'label':('左臂' if item.startswith('left') else '右臂')+' '+labels[item.split('_',1)[1]],
                        'message':'尚未录制：'+('左臂' if item.startswith('left') else '右臂')+' '+labels[item.split('_',1)[1]]}
                       for item in sorted(required-set(self.captures))]
        short_ranges=[{'key':key,'label':joint_label(*split_key(key)),
                       'message':joint_label(*split_key(key))+'行程不足48计数'}
                      for key in KEYS if self.samples and self.samples[key].high-self.samples[key].low<48]
        unconfirmed=[{'key':key,'label':joint_label(*split_key(key)),
                      'message':joint_label(*split_key(key))+'行程尚未确认'}
                     for key in KEYS if key not in self.confirmed_keys]
        blockers=[*missing_cards,*short_ranges,*unconfirmed]
        return {'active':self.active,'id':self.id,'step':self.step,'error':self.error,'applied':self.applied,
                'arm_ports':self.runtime.arm_ports(),
                'ranges':[{'key':key,'label':joint_label(*split_key(key)),**s.snapshot()} for key,s in self.samples.items()],
                'captured':list(self.captures),'confirmed_keys':self.confirmed_keys,
                'base_running':self.base_running,'base_tests':deepcopy(self.base_tests),'wheels':self.wheels,
                'design_geometry':DESIGN_GEOMETRY,'recovery_reset':self.recovery_reset,
                'recoverable':self.find_recoverable() if not self.active or self.error else None,
                'merge_checkpoint_id':self._best_card_checkpoint() if self.active and self.step=='cards' and not self.captures else None,
                'step_warnings':self.step_warnings,'persistence_error':self.persistence_error,
                'required_cards':sorted(required),'apply_blockers':blockers,'apply_ready':self.active and not blockers}
