"""Single serial owner, live native gamepad input and bounded joint control.

Uses the project's STS3215 protocol/map; retains device homing and limits.
The old synthetic +/-512 calibration is not a physical range calibration.
"""
from __future__ import annotations

from collections import deque
import datetime as dt
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from .torque_control import TorqueSession, EXPECTED, read_registers, word, write_register

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "evidence" / dt.date.today().isoformat()
PROFILE = ROOT / "device-calibration.json"
BUTTON_NAMES = ['A', 'B', 'X', 'Y', 'LB', 'RB', 'View', 'Menu', 'LS', 'RS', 'LT', 'RT',
                '十字键上', '十字键下', '十字键左', '十字键右', 'Xbox']
JOINT_LABELS = ['肩水平', '肩抬升', '肘', '腕俯仰', '腕旋转', '夹爪']
EXPECTED_SERIALS = {'COM3': '5B61034756', 'COM4': '5B61034338'}


def now() -> str:
    return dt.datetime.now().astimezone().isoformat()


def joint_label(port: str, motor_id: int) -> str:
    if motor_id <= 6:
        return ('左臂' if port == 'COM3' else '右臂') + JOINT_LABELS[motor_id-1]
    if port == 'COM3':
        return '头部' + str(motor_id-6)
    return '左轮' if motor_id == 9 else '右轮'


def normalize_pad(raw: dict) -> dict:
    """SDL and replay use exactly the same semantic input mapping."""
    buttons = raw.get('buttons', [])
    hat = raw.get('hat', [0, 0])
    lb_index=raw.get('lb_index',4); back_index=raw.get('back_index',6)
    return {'connected': bool(raw.get('connected')), 'lb': len(buttons)>lb_index and bool(buttons[lb_index]),
            'back': len(buttons)>back_index and bool(buttons[back_index]), 'hat': tuple(hat)}


class ControlRuntime:
    def __init__(self, session=None):
        self.io = threading.RLock()
        self.session = session or TorqueSession()
        self.events = deque(maxlen=300)
        self.event_id = 0
        self.event_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.controller = {'controller': 'offline', 'armed': False, 'torque': False,
                           'paused': False, 'pause_reason': None,
                           'selected_joint': None, 'last_action': '观测中，尚未启动手柄接管',
                           'updated_at': now(), 'error': None}
        self.gamepad = {'connected': False, 'devices': [], 'buttons': [], 'axes': [], 'hat': [0,0],
                        'updated_at': None, 'source': 'Windows XInput 原生输入'}
        self.pad_time = 0.0
        self.report = {'ports': [], 'updated_at': None}
        self.profile = None
        self.envelopes = {}
        self.selected = ('COM4', 1)
        self.previous = {'connected': False, 'lb': False, 'back': False, 'hat': (0,0)}
        self.active = None
        self.pending = None
        self.last_command = None
        self.notice = None
        self.step_raw = 16
        self.speed_raw = 100
        self.ui_heartbeat = time.monotonic()
        self.diagnostic_replay = False
        self.camera_health = [{'camera':i,'name':f'USB2.0_CAM1 · {i+1:02}',
                               'state':'等待首帧','frames':0} for i in range(3)]

    def log(self, kind: str, message: str, **fields):
        with self.event_lock:
            self.event_id += 1
            event = {'seq':self.event_id, 'time':now(), 'kind':kind, 'message':message, **fields}
            self.events.append(event)
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with (LOG_DIR/'control-events.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps(event,ensure_ascii=False)+'\n')
        return event

    def start(self):
        self.log('startup','控制台 v2：单串口所有者 / Windows XInput；输入、命令、编码器反馈独立记录')
        threading.Thread(target=self._monitor,daemon=True,name='hardware-owner').start()
        threading.Thread(target=self._pad_loop,daemon=True,name='gamepad-owner').start()

    def _check_ports(self):
        import serial.tools.list_ports
        ports={p.device:p.serial_number for p in serial.tools.list_ports.comports()}
        for name, identity in EXPECTED_SERIALS.items():
            if ports.get(name) != identity:
                raise RuntimeError(f'{name} 身份不匹配：预期 {identity}，当前 {ports.get(name)}')

    def scan(self):
        self._check_ports()
        self.session.open()
        ports=[]
        for name, ids in EXPECTED.items():
            motors=[]
            for motor_id in ids:
                r=read_registers(self.session.ports[name],motor_id)
                if word(r,3)!=777:
                    raise RuntimeError(f'{name}:{motor_id} 型号不是 STS3215')
                motors.append({'id':motor_id,'label':joint_label(name,motor_id),'model':777,
                               'model_name':'STS3215','torque':r[40],'mode':r[33],
                               'position':word(r,56),'goal':word(r,42),'speed':word(r,58),
                               'goal_velocity':word(r,46),'voltage':r[62]/10,'temperature':r[63],
                               'min':word(r,9),'max':word(r,11),'homing_raw':word(r,31),
                               'load_raw':word(r,60),'status':r[65],'error':0})
            ports.append({'port':name,'connected':True,'error':None,'motor_count':len(motors),'motors':motors})
        self.report={'ports':ports,'updated_at':now()}

    def _monitor(self):
        while not self.stop_event.is_set():
            try:
                with self.io:
                    self.scan()
                    if self.profile is None:
                        self.capture_profile()
                    self.check_watchdog()
                    if self.pending:
                        self._check_feedback()
            except Exception as exc:
                with self.io:
                    if self.controller['controller']=='online':
                        self._pause('通信异常：'+str(exc))
                    old_error=self.controller.get('error')
                    self.controller['error']=str(exc)
                    self.report={'ports':[],'updated_at':now(),'error':str(exc)}
                    self.session.close()
                    if old_error!=str(exc): self.log('error',str(exc))
            self.stop_event.wait(.35)

    def check_watchdog(self):
        if self.controller['controller']!='online' or self.controller['paused']:return
        if time.monotonic()-self.pad_time>.5:
            self._pause('手柄输入超过500ms未更新')
        elif time.monotonic()-self.ui_heartbeat>3:
            self._pause('网页心跳超过3秒未更新；接管保留，请回到页面手动恢复')

    def inform(self, code, message, level='info', **fields):
        self.notice={'code':code,'message':message,'level':level,'time':now(),**fields}
        self.controller.update(last_action=message,updated_at=now())
        self.log(code,message,**fields)

    def capture_profile(self):
        """Read device calibration. Never write min/max or homing to EEPROM."""
        self.scan()
        joints={}
        for port in self.report['ports']:
            for m in port['motors']:
                joints[f"{port['port']}:{m['id']}"]={k:m[k] for k in
                    ('id','label','min','max','homing_raw','position','mode')}
        result={'schema':2,'kind':'device_register_snapshot','captured_at':now(),
                'serials':EXPECTED_SERIALS,'full_manual_range_verified':False,'joints':joints}
        PROFILE.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        self.profile=result
        if self.controller['controller']!='online':
            self._make_envelopes()
        self.log('calibration','已读取16个舵机实际限位/偏移；未修改限位，不等同于完整人工校准')
        return result

    def _make_envelopes(self):
        self.envelopes={}
        for port in self.report['ports']:
            for m in port['motors']:
                if port['port']=='COM4' and m['id']>=9: continue
                low=max(m['min'],m['position']-96)
                high=min(m['max'],m['position']+96)
                available=high-low>=24 and low-8<=m['position']<=high+8
                self.envelopes[(port['port'],m['id'])]={'min':low,'max':high,'available':available,
                    'device_min':m['min'],'device_max':m['max'],
                    'label':m['label'],'initial':m['position'],
                    'reason':None if available else '设备保存的行程过窄或当前位置不在范围内，需人工限位校准'}

    def begin(self, confirmed: bool):
        with self.io:
            if not confirmed: raise ValueError('请先确认小范围手臂测试和线路安全')
            if not self.gamepad['connected'] or time.monotonic()-self.pad_time>.5:
                raise ValueError('原生手柄输入未在线')
            if normalize_pad(self.gamepad)['lb']:
                raise ValueError('请先松开LB，再启动；防止接管时沿用已按住的使能键')
            if self.controller['controller']=='online': return self.controller.copy()
            if self.active:self._hold()
            self.capture_profile()
            self._make_envelopes()
            options=[key for key,value in self.envelopes.items() if value['available']]
            if not options: raise ValueError('没有可安全小步测试的关节，需人工校准')
            if self.selected not in options: self.selected=options[0]
            self.previous=normalize_pad(self.gamepad)
            self.previous_buttons=list(self.gamepad.get('buttons',[]))
            self.notice=None
            self.last_command=None
            self.ui_heartbeat=time.monotonic()
            self.controller.update(controller='online',armed=False,paused=False,pause_reason=None,error=None,
                selected_joint=self.envelopes[self.selected]['label'],updated_at=now(),
                last_action='接管成功，等待新的LB按下；方向键一次一小步')
            self.log('controller_start',self.controller['last_action'],selected=list(self.selected))
            return self.controller.copy()

    def _hold(self):
        if self.active:
            name,motor_id=self.active
            p=self.session.ports[name]
            r=read_registers(p,motor_id)
            write_register(p,motor_id,42,word(r,56),2)
            self.active=None
        if self.pending and self.last_command:
            self.last_command.update(outcome='interrupted',message='动作已停止并保持，未把中断当作完成')
        self.pending=None
        self.controller['active_joint']=None

    def _pause(self, reason):
        """Fault stops motion but never cancels the user's takeover session."""
        if self.controller['paused']:return
        try:self._hold()
        except Exception as exc:
            reason+='；保持指令未确认：'+str(exc)
            if self.last_command:self.last_command.update(outcome='hold_unconfirmed',message=reason)
            self.pending=None
        self.controller.update(armed=False,paused=True,pause_reason=reason,active_joint=None,
                               last_action=reason,updated_at=now())
        self.inform('fault_pause',reason,'error')

    def resume(self):
        with self.io:
            if self.controller['controller']!='online':raise ValueError('先开启接管')
            if not self.controller['paused']:return self.controller.copy()
            if not self.gamepad['connected'] or time.monotonic()-self.pad_time>.5:
                raise ValueError('手柄输入尚未恢复')
            state=normalize_pad(self.gamepad)
            buttons=self.gamepad.get('buttons',[])
            if state['lb'] or any(state['hat']) or (len(buttons)>1 and buttons[1]):
                raise ValueError('先松开LB、B和十字键，再恢复')
            self.scan()
            # Retry an unconfirmed hold after communication recovers. Never
            # resume while a prior goal may still be latched in the servo.
            self._hold()
            self.previous=state
            self.previous_buttons=list(buttons)
            self.ui_heartbeat=time.monotonic()
            self.controller.update(armed=False,paused=False,pause_reason=None,error=None,updated_at=now())
            self.notice=None
            self.inform('resume','接管已恢复，等待重新按住LB；没有续发旧指令')
            return self.controller.copy()

    def _stop(self, reason):
        hold_error=None
        try: self._hold()
        except Exception as exc:
            reason += ';保持指令失败：'+str(exc)
            hold_error=reason
            self.pending=None
            if self.last_command:self.last_command.update(outcome='hold_unconfirmed',message=reason)
        self.controller.update(controller='offline',armed=False,paused=False,pause_reason=None,active_joint=None,
                               last_action=reason,updated_at=now())
        self.notice=None
        if hold_error:self.inform('error',hold_error,'error')
        self.log('controller_stop',reason)

    def stop(self):
        with self.io:
            self._stop('已停止手柄接管，保留现有扭矩保持，避免手臂下落')
            return self.controller.copy()

    def select(self, name, motor_id):
        with self.io:
            key=(name,int(motor_id))
            if key not in self.envelopes: raise ValueError('当前仅开放手臂和头部小步测试')
            if not self.envelopes[key]['available']: raise ValueError(self.envelopes[key]['reason'])
            self._hold()
            self.notice=None
            self.selected=key
            self.controller.update(selected_joint=self.envelopes[key]['label'],updated_at=now())
            self.log('select','选中'+self.envelopes[key]['label'],port=name,id=motor_id)

    def _move(self, direction: int, source='native_gamepad'):
        if not self.controller['armed']: raise ValueError('LB未按住，动作被拒绝')
        if self.controller['paused']:raise ValueError('动作已暂停，请先恢复')
        if self.pending:
            self.inform('blocked','上一条命令等待编码器反馈，未叠加新目标；接管保留',waiting_for_feedback=True);return None
        name,motor_id=self.selected
        limits=self.envelopes[self.selected]
        if not limits['available']: raise ValueError(limits['reason'])
        p=self.session.ports[name]
        r=read_registers(p,motor_id)
        origin=word(r,56)
        if not 10<=r[62]/10<=13 or r[63]>=55:
            self._pause('供电或温度超出小步测试范围');return None
        requested=origin+direction*self.step_raw
        target=max(limits['min'],min(limits['max'],requested))
        if (target-origin)*direction<4 or abs(target-origin)>32:
            self.inform('limit','已到'+('正向' if direction>0 else '反向')+'边界；接管保留，可反向小步或切换关节','warning',
                        port=name,id=motor_id,origin=origin,requested=requested,limits=limits,direction=direction)
            return None
        if r[33]!=0: raise ValueError('关节不是位置模式，未自动修改模式')
        # No old target is ever energized: latch current position first.
        write_register(p,motor_id,42,origin,2)
        write_register(p,motor_id,46,self.speed_raw,2)
        if r[40]!=1: write_register(p,motor_id,40,1,1)
        write_register(p,motor_id,42,target,2)
        self.active=self.selected
        self.pending={'port':name,'id':motor_id,'origin':origin,'target':target,
                      'at':time.monotonic(),'source':source}
        self.last_command={k:v for k,v in self.pending.items() if k!='at'}
        self.last_command.update(label=limits['label'],time=now(),outcome='pending',message='目标已发送，等待读取编码器')
        self.notice=None
        if target!=requested:
            self.inform('limit','本步已缩短至边界；接管保留，下一步可反向或切换关节','warning',
                        port=name,id=motor_id,origin=origin,target=target,requested=requested,direction=direction)
        self.controller.update(active_joint=limits['label'],last_action=f'{limits["label"]}：{origin} → {target}',updated_at=now())
        self.log('command',self.controller['last_action'],port=name,id=motor_id,origin=origin,
                 target=target,speed=self.speed_raw,source=source)
        return self.pending.copy()

    def _check_feedback(self):
        pending=self.pending
        if not pending or time.monotonic()-pending['at']<.45:return
        r=read_registers(self.session.ports[pending['port']],pending['id'])
        pos=word(r,56); moved=abs(pos-pending['origin'])>=3
        self.log('feedback' if moved else 'no_motion',
                 f'编码器反馈：{pending["origin"]} → {pos}，目标 {pending["target"]}',
                 port=pending['port'],id=pending['id'],origin=pending['origin'],position=pos,
                 target=pending['target'],moved=moved,source=pending['source'])
        reached=abs(pos-pending['target'])<=6
        self.last_command.update(position=pos,moved=moved,outcome='feedback' if moved else 'no_motion',
                                 reached=reached,message='编码器已变化'+('，进入目标容差' if reached else '，目标尚有偏差') if moved else '编码器未变化')
        self.controller['last_action']=f'编码器已变化至 {pos}' if moved else '已写目标但编码器未变化，动作暂停，接管保留'
        if self.notice and self.notice.get('waiting_for_feedback'):self.notice=None
        self.pending=None
        if not moved:self._pause('目标已写入但编码器未变化；接管保留，请检查日志后手动恢复')

    def process_input(self, raw, source='native_gamepad'):
        """Native/replay share edge detection and the command boundary."""
        current=normalize_pad(raw)
        old=self.previous
        self.previous=current
        with self.io:
            if self.controller['controller']!='online':return
            if not current['connected']:
                self._pause('手柄断开；接管保留，连接恢复后请手动恢复动作');return
            if current['back']:
                self._stop('View键停止接管');return
            if self.controller['paused']:return
            buttons=raw.get('buttons',[])
            previous_buttons=getattr(self,'previous_buttons',[])
            self.previous_buttons=list(buttons)
            b_edge=len(buttons)>1 and buttons[1] and not (len(previous_buttons)>1 and previous_buttons[1])
            if b_edge:
                self._hold()
                self.controller.update(armed=False,updated_at=now())
                self.inform('hold','B：本次动作停止并保持；接管保留，松开LB再按可继续')
                return
            if len(buttons)>1 and buttons[1]:return
            if current['lb']!=old['lb']:
                if not current['lb']:self._hold()
                self.controller.update(armed=current['lb'],updated_at=now(),
                    last_action='LB已按住：允许一次一步' if current['lb'] else 'LB已松开：停止并保持姿态')
                self.log('lb',self.controller['last_action'],pressed=current['lb'],source=source)
            x,y=current['hat']; px,py=old['hat']
            if x and y:
                if (x,y)!=(px,py):self.inform('blocked','十字键斜向输入不执行；请松开后单独点按一个方向')
                return
            if self.controller['armed'] and x and x!=px:
                options=[key for key,value in self.envelopes.items() if value['available']]
                self.select(*options[(options.index(self.selected)+(1 if x>0 else -1))%len(options)])
            if y and y!=py:
                if self.controller['armed']:self._move(1 if y>0 else -1,source)
                else:self.inform('blocked','十字键已收到，但LB没有按住；未发送动作',source=source)
            self.controller['updated_at']=now()

    def _pad_loop(self):
        from .gamepad_input import XboxReader
        try:
            reader=XboxReader()
        except Exception as exc:
            self.gamepad={'connected':False,'devices':[],'error':str(exc),'buttons':[],'axes':[],'hat':[0,0]}
            self.log('error','XInput初始化失败：'+str(exc));return
        last_signature=None;last_axis=None
        while not self.stop_event.is_set():
            try:
                raw=reader.read()
                raw.update(updated_at=now(),source='Windows XInput / 实际控制通道')
                self.gamepad=raw;self.pad_time=time.monotonic()
                signature=(raw['connected'],tuple(raw['buttons']),tuple(raw['hat']))
                axes=tuple(round(x,1) for x in raw['axes'])
                if signature!=last_signature:
                    self.log('input','手柄按键/方向状态变化',buttons=raw['buttons'],hat=raw['hat'],connected=raw['connected'])
                    last_signature=signature
                if axes!=last_axis:
                    self.log('axes','摇杆/扳机变化（只显示，未映射动作）',axes=raw['axes']);last_axis=axes
                if not self.diagnostic_replay:self.process_input(raw)
            except Exception as exc:
                with self.io:
                    if self.controller['controller']=='online':self._pause('手柄输入异常：'+str(exc))
                self.gamepad={'connected':False,'devices':[],'error':str(exc),'buttons':[],'axes':[],'hat':[0,0]}
            self.stop_event.wait(.025)

    def torque(self, enabled, selection=None):
        with self.io:
            if self.controller['controller']=='online':
                raise ValueError('先停止手柄接管，再切换扭矩')
            # Retain operating modes and PID values. Wheels stay at speed zero.
            self.session.open(); results=[]
            for name,ids in (selection or EXPECTED).items():
                if name not in EXPECTED or any(i not in EXPECTED[name] for i in ids):raise ValueError('未知电机')
                for i in ids:
                    p=self.session.ports[name];r=read_registers(p,i)
                    if enabled:
                        if r[33]==0:write_register(p,i,42,word(r,56),2)
                        elif name=='COM4' and i in (9,10):write_register(p,i,46,0,2)
                        else:raise ValueError('未知关节模式，未开启')
                    write_register(p,i,40,int(enabled),1)
                    q=read_registers(p,i)
                    results.append({'port':name,'id':i,'torque':q[40]})
            self.scan()
            self.log('torque',f'{"打开" if enabled else "关闭"}{len(results)}个扭矩',motors=results)
            return {'status':'ok','count':len(results),'message':f'已回读{len(results)}个扭矩', 'motors':results}

    def snapshot(self):
        with self.io:
            result=json.loads(json.dumps(self.report))
            motors=[m for p in result['ports'] for m in p['motors']]
            c=self.controller.copy();c['torque']=any(m['torque'] for m in motors)
            c['selected_joint']=joint_label(*self.selected)
            result.update(mode='BOUNDED_CONTROL' if c['controller']=='online' else 'OBSERVE',
                controller=c,gamepad=self.gamepad,calibration={'exists':self.profile is not None,
                'kind':'device_register_snapshot','full_range_verified':False},
                cameras=[dict(x) for x in self.camera_health],version='2.1-control-map',
                notice=self.notice,last_command=self.last_command,
                joints=[{'port':p,'id':i,**v} for (p,i),v in self.envelopes.items()],
                settings={'step_raw':self.step_raw,'speed_raw':self.speed_raw,'session_limit':96},
                input_age_ms=round((time.monotonic()-self.pad_time)*1000) if self.pad_time else None)
            return result

    def close(self):
        self.stop_event.set()
        with self.io:
            self._stop('服务停止，保持当前姿态')
            self.session.close()
