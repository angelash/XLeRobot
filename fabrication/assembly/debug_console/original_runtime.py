"""Guarded upstream Xbox controller, sharing the diagnostic serial owner."""
from __future__ import annotations
import json
import time
import threading
from pathlib import Path
from .control_runtime import ControlRuntime,EXPECTED_SERIALS,ROOT,now
from .torque_control import EXPECTED,read_registers,word,write_register
from .upstream_teleop import UpstreamTeleop,JOINTS,intentions,neutral,encode_velocity

KINEMATIC_PROFILE=ROOT/'kinematic-calibration.json'
SERVICE_PROFILE=ROOT.parents[1]/'.local-deploy'/'hardware-service.json'


class OriginalRuntime(ControlRuntime):
    def __init__(self,session=None):
        super().__init__(session)
        self.control_profile='diagnostic'
        self.engine=UpstreamTeleop()
        self.tick_time=0.;self.last_original_notice=None;self.last_notice_at=0.
        self.moving={};self.feedback_window={};self.direct_targets={}
        self.wheels_commanded=False;self.wheel_goals=[0,0]
        self.kinematic_profile={};self.grip_touched={'left':False,'right':False}
        self.last_menu=False
        self.notice_times={}
        self.preparation=None
        self.service_blocked={}
        self.wheel_pulse_timer=None;self.wheel_pulse_latched=False

    def _make_envelopes(self):
        # Reload maintenance exclusions on every fresh capture, including after
        # a service restart and when entering the older diagnostic controller.
        self.service_blocked={}
        if SERVICE_PROFILE.is_file():
            profile=json.loads(SERVICE_PROFILE.read_text(encoding='utf-8-sig'))
            if profile.get('serials')!=EXPECTED_SERIALS:raise ValueError('实机检修记录的USB身份不匹配')
            blocked=profile.get('blocked_motors',{})
            valid_keys={f'{port}:{i}' for port,ids in EXPECTED.items() for i in ids}
            if not isinstance(blocked,dict) or not set(blocked)<=valid_keys or not all(isinstance(v,str) and v for v in blocked.values()):
                raise ValueError('实机检修记录格式错误，未开放动作')
            self.service_blocked=blocked
        super()._make_envelopes()
        for (port,i),envelope in self.envelopes.items():
            reason=self.service_blocked.get(f'{port}:{i}')
            if reason:envelope.update(available=False,reason=reason,maintenance_blocked=True)

    def torque(self,enabled,selection=None):
        with self.io:
            if enabled:
                if self.controller['controller']=='online':raise ValueError('先停止手柄接管，再切换扭矩')
                self._make_envelopes()
                blocked=[key for key in self.service_blocked
                         if selection is None or (key.split(':')[0] in selection and
                            int(key.split(':')[1]) in selection[key.split(':')[0]])]
                if blocked:raise ValueError('检修关节禁止开启扭矩：'+', '.join(blocked)+'；请使用“准备实机”跳过待检项')
            return super().torque(enabled,selection)

    def prepare_hardware(self,confirmed):
        """Explicitly latch present positions before enabling usable servos."""
        with self.io:
            if not confirmed:raise ValueError('请确认机械臂已支撑且周围可做小幅动作')
            if self.controller['controller']=='online':raise ValueError('先结束当前接管，再准备实机')
            self.capture_profile();ready=[];blocked=[]
            for port in self.report['ports']:
                name=port['port'];p=self.session.ports[name]
                for motor in port['motors']:
                    i=motor['id'];wheel=name=='COM4' and i in (9,10)
                    if f'{name}:{i}' in self.service_blocked:
                        blocked.append({'port':name,'id':i,'reason':self.service_blocked[f'{name}:{i}']});continue
                    if wheel:
                        if motor['mode']!=1:blocked.append({'port':name,'id':i,'reason':'双轮需要现有速度模式1'});continue
                    elif (not self.envelopes[(name,i)]['available'] or
                          not motor['min']<=motor['position']<=motor['max'] or motor['mode']!=0):
                        blocked.append({'port':name,'id':i,'reason':'设备范围/当前位置/模式未就绪'});continue
                    if not 10<=motor['voltage']<=13 or motor['temperature']>=50:
                        blocked.append({'port':name,'id':i,'reason':'供电或温度未就绪'});continue
                    r=read_registers(p,i)
                    if wheel:write_register(p,i,46,0,2)
                    else:
                        pos=word(r,56)
                        if not motor['min']<=pos<=motor['max']:raise ValueError('准备期间位置超出已读取范围')
                        write_register(p,i,46,60,2)
                        write_register(p,i,42,pos,2)
                    write_register(p,i,40,1,1)
                    after=read_registers(p,i)
                    if after[40]!=1 or after[33]!=(1 if wheel else 0):raise RuntimeError('扭矩/模式回读失败')
                    ready.append({'port':name,'id':i,'position':word(after,56),'torque':after[40]})
            self.scan()
            self.preparation={'ready':ready,'blocked':blocked,'prepared_at':now(),
                              'message':'已保持当前位置；未归零，未修改模式、PID或设备限位'}
            self.log('hardware_prepare',self.preparation['message'],ready=ready,blocked=blocked)
            return self.preparation

    def begin(self,confirmed):
        if self.controller['controller']=='online' and self.control_profile!='diagnostic':
            raise ValueError('先由你结束原版接管，再切换单关节调试')
        if self.control_profile!='diagnostic':self._hold()
        self.control_profile='diagnostic'
        return super().begin(confirmed)

    def select(self,name,motor_id):
        if self.control_profile!='diagnostic' and self.controller['controller']=='online':
            raise ValueError('原版映射按左右臂控制；请先由你结束接管再使用单关节选择')
        return super().select(name,motor_id)

    def load_kinematic_profile(self):
        self.kinematic_profile={}
        if KINEMATIC_PROFILE.is_file():
            try:
                data=json.loads(KINEMATIC_PROFILE.read_text(encoding='utf-8'))
                if data.get('serials')!=EXPECTED_SERIALS:raise ValueError('标定串口身份不匹配')
                wheels=data.get('wheels',{})
                if wheels.get('verified') and not (.02<=float(wheels.get('radius_m',0))<=.2 and .1<=float(wheels.get('track_m',0))<=1):
                    raise ValueError('双轮验收需记录实测轮径半径与轮距')
                for key,item in data.get('joints',{}).items():
                    if not item.get('verified'):continue
                    if key.endswith(':6'):
                        ends=[float(item['open_raw']),float(item['closed_raw'])]
                        if abs(ends[0]-ends[1])<24 or not all(item['min']<=x<=item['max'] for x in ends):
                            raise ValueError('夹爪需实测开合端点且在设备范围内')
                        continue
                    if item.get('direction') not in (-1,1):raise ValueError('标定方向必须为±1')
                    if not 0<=float(item['reference_raw'])<=4095:raise ValueError('参考编码器错误')
                    if not -180<=float(item['reference_deg'])<=180:raise ValueError('参考角度错误')
                self.kinematic_profile=data
            except (OSError,ValueError,KeyError,TypeError) as exc:
                self.original_notice('calibration','运动学标定不可用：'+str(exc))

    def check_watchdog(self):
        super().check_watchdog()
        if self.control_profile in ('original_hardware','original_joint') and not self.controller['paused'] and self.feedback_window:
            self._check_feedback()

    def motors(self):
        return {(p['port'],m['id']):m for p in self.report['ports'] for m in p['motors']}

    def reference(self,port,i):
        value=self.kinematic_profile.get('joints',{}).get(f'{port}:{i}')
        motor=self.motors().get((port,i))
        if not value or not value.get('verified') or not motor:return None
        if any(value.get(k)!=motor[k] for k in ('min','max','homing_raw')):return None
        return value

    def joint_readiness_reasons(self,motor,envelope):
        label=motor['label'];reasons=[]
        if not envelope.get('available') and not envelope.get('maintenance_blocked'):
            span=motor['max']-motor['min']
            if span<24:
                reasons.append(f'{label}设备行程仅{span}计数（{span*360/4096:.2f}°），需人工核对真实行程')
            elif not motor['min']<=motor['position']<=motor['max']:
                reasons.append(f'{label}实测{motor["position"]}超出设备范围{motor["min"]}–{motor["max"]}')
            else:reasons.append(f'{label}本轮可用行程不足24计数，需核对限位与当前位置')
        if motor['mode']!=0:reasons.append(f'{label}当前模式{motor["mode"]}，需要位置模式0')
        if not motor['torque']:reasons.append(f'{label}扭矩关闭')
        return reasons

    def readiness(self):
        channels={};motors=self.motors()
        for side,port in [('left','COM3'),('right','COM4')]:
            arm_channels=([('lift',[2],False),('elbow',[3],False)] if self.control_profile=='original_joint'
                          else [('position',[2,3,4],True)])
            for channel,ids,needs_ref in arm_channels+[('pan',[1],False),('pitch',[4],False),('roll',[5],False),('grip',[6],True)]:
                reasons=[]
                for i in ids:
                    m=motors.get((port,i));e=self.envelopes.get((port,i))
                    if not m or not e:reasons.append(f'{port}:{i}未读取');continue
                    reasons.extend(self.joint_readiness_reasons(m,e))
                    if f'{port}:{i}' in self.service_blocked:reasons.append(self.service_blocked[f'{port}:{i}'])
                    if needs_ref and self.control_profile!='original_joint' and not self.reference(port,i):reasons.append(f'{m["label"]}运动学零位/方向未确认')
                channels[f'{side}_{channel}']={'ready':not reasons,'reasons':reasons}
        for i in (7,8):
            m=motors.get(('COM3',i));e=self.envelopes.get(('COM3',i),{})
            reasons=[]
            if not m:reasons.append(f'COM3:{i}未读取')
            else:reasons.extend(self.joint_readiness_reasons(m,e))
            channels[f'head_{i}']={'ready':not reasons,'reasons':reasons}
        reasons=[]
        if self.control_profile!='original_joint' and not self.kinematic_profile.get('wheels',{}).get('verified'):
            reasons.append('双轮离地模式/方向/零速尚未验收')
        for i in (9,10):
            m=motors.get(('COM4',i))
            if not m or m['mode']!=1 or not m['torque']:reasons.append(f'COM4:{i}需要速度模式且扭矩开启（不自动修改）')
        channels['base']={'ready':not reasons,'reasons':reasons}
        return channels

    def begin_original(self,target,confirmed):
        with self.io:
            if target not in ('preview','hardware','joint'):raise ValueError('未知控制目标')
            if self.controller['controller']=='online':raise ValueError('请先由你结束当前接管，再切换控制方式')
            if not confirmed:raise ValueError('请确认当前控制目标和周围安全')
            if time.monotonic()-self.pad_time>.5 or not neutral(self.gamepad):
                raise ValueError('请连接手柄并松开所有按键、扳机，将摇杆回中')
            if target in ('hardware','joint'):
                self.capture_profile();self._make_envelopes();self.load_kinematic_profile()
                if not any(m['torque'] for p in self.report['ports'] for m in p['motors']):
                    raise ValueError('实机扭矩全部关闭：先点击“准备实机 · 保持当前位置”，再开启接管')
                if target=='joint':
                    for key,e in self.envelopes.items():
                        e['min']=max(e['min'],e['initial']-32);e['max']=min(e['max'],e['initial']+32)
            self.control_profile='original_'+target
            angles=None
            if target=='hardware':
                angles={s:dict(zip(JOINTS,[0,0,2,-2,0,90])) for s in ('left','right')}
                for side,port in [('left','COM3'),('right','COM4')]:
                    for i,j in enumerate(JOINTS,1):
                        ref=self.reference(port,i)
                        if ref:
                            raw=self.motors()[(port,i)]['position']
                            angles[side][j]=(2+(raw-ref['closed_raw'])*88/(ref['open_raw']-ref['closed_raw'])) if i==6 else ref['reference_deg']+(raw-ref['reference_raw'])*ref['direction']*360/4096
            wheels=self.kinematic_profile.get('wheels',{}) if target=='hardware' else {}
            if not wheels.get('verified'):wheels={}
            self.engine=UpstreamTeleop(angles,wheels.get('radius_m',.05),wheels.get('track_m',.25))
            if target=='hardware':
                for side,port in [('left','COM3'),('right','COM4')]:
                    for i,j in enumerate(JOINTS,1):
                        ref=self.reference(port,i);e=self.envelopes.get((port,i))
                        if ref and e:
                            values=[2+(v-ref['closed_raw'])*88/(ref['open_raw']-ref['closed_raw']) for v in (e['min'],e['max'])] if i==6 else [ref['reference_deg']+(v-ref['reference_raw'])*ref['direction']*360/4096 for v in (e['min'],e['max'])]
                            self.engine.bounds[side][j]=(min(values),max(values))
            self.tick_time=time.monotonic();self.ui_heartbeat=self.tick_time
            self.direct_targets={};self.feedback_window={};self.moving={}
            self.grip_touched={'left':False,'right':False};self.last_menu=False
            self.notice=None;self.last_command=None
            self.controller.update(controller='online',paused=False,pause_reason=None,armed=True,error=None,
                last_action='原版Xbox映射已接管；'+('仅运动学预览，不发设备指令' if target=='preview' else '受限实机；不可用通道只提示'),updated_at=now())
            self.log('controller_start',self.controller['last_action'],profile=self.control_profile)
            return self.controller.copy()

    def original_notice(self,code,message):
        stamp=time.monotonic();key=(code,message)
        if stamp-self.notice_times.get(key,-10)>2:
            self.notice_times[key]=stamp
            self.last_original_notice=key;self.last_notice_at=stamp
            self.inform(code,message,'warning')

    def _hold(self):
        if self.control_profile=='diagnostic':return super()._hold()
        if self.wheel_pulse_timer:self.wheel_pulse_timer.cancel();self.wheel_pulse_timer=None
        failures=[]
        # Attempt every active joint and both wheels even if one port fails.
        if self.control_profile in ('original_hardware','original_joint'):
            for (port,i) in list(self.moving):
                try:
                    p=self.session.ports[port];r=read_registers(p,i)
                    write_register(p,i,42,word(r,56),2);self.moving.pop((port,i),None)
                except Exception as exc:failures.append(f'{port}:{i} '+str(exc))
            if self.wheels_commanded:
                for i in (9,10):
                    try:write_register(self.session.ports['COM4'],i,46,0,2)
                    except Exception as exc:failures.append(f'轮{i} '+str(exc))
                if not failures:self.wheels_commanded=False
        self.engine.wheels=[0,0];self.direct_targets={};self.feedback_window={}
        self.grip_touched={'left':False,'right':False}
        if self.last_command and self.last_command.get('outcome')=='pending':self.last_command['outcome']='interrupted'
        if failures:raise RuntimeError('；'.join(failures))

    def resume(self):
        if self.control_profile=='diagnostic':return super().resume()
        with self.io:
            if self.controller['controller']!='online':raise ValueError('先开启接管')
            if not self.controller['paused']:return self.controller.copy()
            if time.monotonic()-self.pad_time>.5 or not neutral(self.gamepad):raise ValueError('先将全部输入回中松开')
            if self.control_profile in ('original_hardware','original_joint'):self.scan()
            self._hold();self.ui_heartbeat=time.monotonic();self.tick_time=self.ui_heartbeat
            if self.control_profile=='original_hardware':
                for side,port in [('left','COM3'),('right','COM4')]:
                    for i,j in enumerate(JOINTS,1):
                        ref=self.reference(port,i)
                        if ref:
                            raw=self.motors()[(port,i)]['position']
                            self.engine.q[side][j]=(2+(raw-ref['closed_raw'])*88/(ref['open_raw']-ref['closed_raw'])) if i==6 else ref['reference_deg']+(raw-ref['reference_raw'])*ref['direction']*360/4096
                self.engine.reset_origins()
            self.controller.update(paused=False,pause_reason=None,armed=True,error=None,updated_at=now())
            self.notice=None;self.original_notice('resume','原版映射已恢复，不重放旧输入')
            return self.controller.copy()

    def process_input(self,raw,source='native_gamepad'):
        if self.control_profile=='diagnostic':return super().process_input(raw,source)
        with self.io:
            if self.controller['controller']!='online':return
            if not raw.get('connected'):self._pause('手柄断开，接管保留');return
            intent=intentions(raw)
            if intent['stop']:self._stop('View结束接管，保持姿态；不执行原脚本归零');return
            menu=intent['pause'];edge=menu and not self.last_menu;self.last_menu=menu
            if edge and not self.controller['paused']:self._pause('Menu暂停所有动作，接管保留；回中后点击恢复');return
            if self.controller['paused']:return
            stamp=time.monotonic();dt=min(.1,max(0,stamp-self.tick_time))
            if dt<.04:return
            self.tick_time=stamp
            self.check_watchdog()
            if self.controller['paused']:return
            previous={s:dict(q) for s,q in self.engine.q.items()}
            if self.control_profile!='original_joint':self.engine.step(raw,dt)
            else:self.engine.last_input=intent
            if self.engine.blockers:self.original_notice('limit','；'.join(self.engine.blockers))
            if self.control_profile=='original_joint':self._execute_joint(intent,dt)
            elif self.control_profile=='original_hardware':
                for side in ('left','right'):
                    if not self.grip_touched[side] and intent[side]['trigger']<=.75:
                        self.engine.q[side]['grip']=previous[side]['grip']
                self._execute(intent,previous,dt)
            self.controller.update(updated_at=now(),last_action='原版Xbox · '+self.control_profile.split('_')[-1]+' · 左'+intent['left']['mode']+' / 右'+intent['right']['mode'])

    def _channel(self,key):
        value=self.readiness()[key]
        if not value['ready']:self.original_notice('blocked',key+'：'+'；'.join(value['reasons']))
        return value['ready']

    def _execute_joint(self,intent,dt):
        """A clearly separate raw-joint mode; no guessed IK references."""
        for side,port in [('left','COM3'),('right','COM4')]:
            item=intent[side];mode=item['mode']
            rates={1:item['h']*5 if mode=='pan' else 0,
                   2:item['h']*5 if mode=='position' else 0,
                   3:-item['v']*5 if mode=='position' else 0,
                   4:-item['v']*5 if mode=='wrist' else 0,
                   5:item['h']*5 if mode=='wrist' else 0}
            # Triggers exercise only this session's bounded grip interval.
            if item['trigger']>.75:self.grip_touched[side]=True
            if self.grip_touched[side]:
                e=self.envelopes.get((port,6),{})
                motor=self.motors().get((port,6),{})
                if e.get('available') and motor.get('torque') and motor.get('mode')==0:
                    self._direct(port,6,5 if item['trigger']>.75 else -5,dt)
            for i,rate in rates.items():
                e=self.envelopes.get((port,i),{});m=self.motors().get((port,i),{})
                if rate and (f'{port}:{i}' in self.service_blocked or not e.get('available') or not m.get('torque') or m.get('mode')!=0):
                    if (port,i) in self.moving:self._hold_one((port,i))
                    reason=self.service_blocked.get(f'{port}:{i}') or e.get('reason') or '请先准备实机并核对模式/扭矩'
                    self.original_notice('blocked',m.get('label',f'{port}:{i}')+'：'+reason);continue
                self._direct(port,i,rate,dt)
        for i,name in [(7,'pan'),(8,'tilt')]:
            rate=intent['head'][name]*5;e=self.envelopes.get(('COM3',i),{});m=self.motors().get(('COM3',i),{})
            if rate and (not e.get('available') or not m.get('torque')):continue
            self._direct('COM3',i,rate,dt)
        active=bool(intent['base']['v'] or intent['base']['omega'])
        if not active:
            if self.wheels_commanded:self._stop_wheel_pulse()
            self.wheel_pulse_latched=False
        elif not self.wheel_pulse_latched:
            motors=self.motors()
            if not all(motors.get(('COM4',i),{}).get('mode')==1 and motors[('COM4',i)]['torque']==1 for i in (9,10)):
                self.original_notice('blocked','双轮尚未准备为零速保持');return
            self.wheel_pulse_latched=True;self.wheels_commanded=True
            from .upstream_teleop import wheel_raw
            raw=wheel_raw(intent['base']['v'],intent['base']['omega'])
            scale=160/max(1,max(abs(v) for v in raw));raw=[round(v*scale) for v in raw]
            self.wheel_pulse_timer=threading.Timer(.18,self._stop_wheel_pulse)
            self.wheel_pulse_timer.daemon=True;self.wheel_pulse_timer.start()
            for i,value in zip((9,10),raw):
                p=self.session.ports['COM4'];r=read_registers(p,i)
                if r[33]!=1 or r[40]!=1 or not 10<=r[62]/10<=13 or r[63]>=50:
                    raise ValueError('双轮模式、扭矩、供电或温度变化，未发送脉冲')
                write_register(p,i,46,encode_velocity(value),2)
            self.engine.wheels=raw;self.wheel_goals=raw
            self.log('wheel_pulse','关节测试模式：双轮最多180ms短脉冲，松开方向键后才可再次触发',raw=raw)

    def _stop_wheel_pulse(self):
        with self.io:
            if self.wheel_pulse_timer:self.wheel_pulse_timer.cancel();self.wheel_pulse_timer=None
            if self.wheels_commanded:
                failures=[]
                for i in (9,10):
                    try:write_register(self.session.ports['COM4'],i,46,0,2)
                    except Exception as exc:failures.append(str(exc))
                if not failures:self.wheels_commanded=False
                self.engine.wheels=[0,0];self.wheel_goals=[0,0]
                if failures:self._pause('双轮短脉冲停车未确认：'+'；'.join(failures))

    def _direct(self,port,i,rate,dt):
        key=(port,i)
        if not rate:
            if key in self.moving:self._hold_one(key)
            self.direct_targets.pop(key,None);return
        if key not in self.direct_targets:
            self.direct_targets[key]=float(word(read_registers(self.session.ports[port],i),56))
        start=self.direct_targets[key]
        self.direct_targets[key]=start+rate*dt*4096/360
        self._goal(port,i,self.direct_targets[key])

    def _hold_one(self,key):
        port,i=key;r=read_registers(self.session.ports[port],i)
        pos=word(r,56);expected=self.moving[key]
        command=self.last_command if self.last_command and (self.last_command.get('port'),self.last_command.get('id'))==key else {}
        origin=self.feedback_window.get(key,{}).get('origin',command.get('origin',pos))
        write_register(self.session.ports[port],i,42,pos,2)
        self.log('feedback',f'{port}:{i}回中保持，实测{pos}',port=port,id=i,position=pos,target=expected,moved=abs(pos-origin)>=1)
        if self.last_command and self.last_command.get('port')==port and self.last_command.get('id')==i:
            self.last_command.update(position=pos,outcome='feedback' if abs(pos-origin)>=1 else 'interrupted',moved=abs(pos-origin)>=1,reached=abs(pos-expected)<=6)
        self.moving.pop(key,None);self.feedback_window.pop(key,None)

    def _goal(self,port,i,value):
        if f'{port}:{i}' in self.service_blocked:raise ValueError(self.service_blocked[f'{port}:{i}'])
        e=self.envelopes[(port,i)];target=round(max(e['min'],min(e['max'],value)))
        if value<e['min'] or value>e['max']:
            self.direct_targets[(port,i)]=float(target)
            self.original_notice('limit',e['label']+'到达本轮边界，可反向；接管保留')
        key=(port,i);p=self.session.ports[port];r=read_registers(p,i);pos=word(r,56)
        if r[33]!=0 or r[40]!=1:raise ValueError(e['label']+'模式/扭矩变化，未自动修改')
        if not 10<=r[62]/10<=13 or r[63]>=55:raise ValueError('供电/温度异常')
        if abs(target-pos)>32:
            self.original_notice('blocked',e['label']+'目标跟随滞后，已停止累积')
            self.direct_targets[key]=float(pos);return False
        if target==pos or self.moving.get(key)==target:return True
        if key not in self.moving:write_register(p,i,42,pos,2)
        # Track before the final write: a missing ACK can still mean the servo
        # accepted the goal, so the fault handler must attempt a hold here.
        self.moving[key]=target
        write_register(p,i,46,60,2)
        write_register(p,i,42,target,2)
        self.feedback_window.setdefault(key,{'origin':pos,'at':time.monotonic()})
        self.last_command={'port':port,'id':i,'label':e['label'],'origin':pos,'target':target,'outcome':'pending','time':now()}
        self.log('command',e['label']+f'：{pos} → {target}',port=port,id=i,origin=pos,target=target,profile=self.control_profile)
        return True

    def _execute(self,intent,previous,dt):
        for side,port in [('left','COM3'),('right','COM4')]:
            item=intent[side];mode=item['mode']
            moving=item['h'] or item['v']
            if mode=='position' and moving:
                if self._channel(side+'_position'):
                    targets=[]
                    for i,j in ((2,'lift'),(3,'elbow'),(4,'wrist')):
                        ref=self.reference(port,i)
                        raw=ref['reference_raw']+(self.engine.q[side][j]-ref['reference_deg'])*4096/360*ref['direction']
                        targets.append((i,raw))
                    # Validate the whole IK step before any joint write.
                    if all(self.envelopes[(port,i)]['min']<=raw<=self.envelopes[(port,i)]['max'] and abs(raw-self.motors()[(port,i)]['position'])<=32 for i,raw in targets):
                        for i,raw in targets:self._goal(port,i,raw)
                    else:
                        self.engine.q[side]=previous[side]
                        self.original_notice('limit',side+'末端步进被关节限位阻挡；可反向，接管保留')
                else:self.engine.q[side]=previous[side]
            else:
                for i in (2,3):
                    if (port,i) in self.moving:self._hold_one((port,i))
            # Direct pan / wrist channels use relative raw increments, no assumed zero.
            for i,channel,rate in [(1,'pan',item['h']*5 if mode=='pan' else 0),
                                   (4,'pitch',-item['v']*5 if mode=='wrist' else 0),
                                   (5,'roll',item['h']*5 if mode=='wrist' else 0)]:
                if i==4 and mode=='position' and moving:continue
                if rate and not self._channel(side+'_'+channel):
                    if (port,i) in self.moving:self._hold_one((port,i))
                    continue
                self._direct(port,i,rate,dt)
            if item['trigger']>.75:self.grip_touched[side]=True
            if self.grip_touched[side]:
                if self._channel(side+'_grip'):
                    ref=self.reference(port,6)
                    raw=ref['closed_raw']+(self.engine.q[side]['grip']-2)/88*(ref['open_raw']-ref['closed_raw'])
                    self._goal(port,6,raw)
                else:self.engine.q[side]['grip']=previous[side]['grip']
        for i,axis_name in [(7,'pan'),(8,'tilt')]:
            rate=intent['head'][axis_name]*5
            if rate and not self._channel(f'head_{i}'):
                if ('COM3',i) in self.moving:self._hold_one(('COM3',i))
                continue
            self._direct('COM3',i,rate,dt)
        raw=list(self.engine.wheels)
        if any(raw):
            if not self._channel('base'):
                if self.wheels_commanded:
                    for i in (9,10):write_register(self.session.ports['COM4'],i,46,0,2)
                    self.wheels_commanded=False;self.wheel_goals=[0,0]
                self.engine.wheels=[0,0];return
            self.wheels_commanded=True
            for i,value in zip((9,10),raw):
                r=read_registers(self.session.ports['COM4'],i)
                if r[33]!=1 or r[40]!=1:raise ValueError('轮子模式/扭矩发生变化')
                if not 10<=r[62]/10<=13 or r[63]>=55:raise ValueError('轮子供电/温度异常')
                write_register(self.session.ports['COM4'],i,46,encode_velocity(value),2)
            self.wheels_commanded=True;self.wheel_goals=raw
        elif self.wheels_commanded:
            for i in (9,10):write_register(self.session.ports['COM4'],i,46,0,2)
            self.wheels_commanded=False;self.wheel_goals=[0,0]

    def _check_feedback(self):
        if self.control_profile=='diagnostic':return super()._check_feedback()
        if self.control_profile not in ('original_hardware','original_joint'):return
        for key,window in list(self.feedback_window.items()):
            if time.monotonic()-window['at']<.8:continue
            port,i=key;pos=word(read_registers(self.session.ports[port],i),56)
            expected=self.moving.get(key,pos);delta=abs(pos-window['origin'])
            reached=abs(pos-expected)<=6
            if not reached and abs(expected-window['origin'])>=4 and delta<2:
                self._pause(f'{port}:{i}原版目标已写入但编码器未变化');return
            self.log('feedback',f'{port}:{i}编码器变化{delta}计数'+('，进入目标容差' if reached else ''),
                     port=port,id=i,position=pos,target=expected,origin=window['origin'],moved=delta>=2,reached=reached)
            if self.last_command and self.last_command.get('port')==port and self.last_command.get('id')==i:
                self.last_command.update(outcome='feedback',position=pos,reached=reached,moved=delta>=2)
            if reached:self.feedback_window.pop(key,None)
            else:self.feedback_window[key]={'origin':pos,'at':time.monotonic()}

    def snapshot(self):
        result=super().snapshot()
        result.update(version='3.1-joint-commissioning',control_profile=self.control_profile,
                      original=self.engine.snapshot(),original_readiness=self.readiness(),hardware_preparation=self.preparation,service_blocked=self.service_blocked)
        return result


class PreviewRuntime(OriginalRuntime):
    """Separate port preview while the user's existing hardware session stays alive."""
    def _monitor(self):
        while not self.stop_event.is_set():
            with self.io:self.check_watchdog()
            self.stop_event.wait(.05)

    def begin_original(self,target,confirmed):
        if target!='preview':raise ValueError('隔离预览服务没有串口写权限，请在主服务切换后使用实机')
        return super().begin_original(target,confirmed)

    def begin(self,confirmed):raise ValueError('隔离预览只支持原版运动学预览')
    def torque(self,*args,**kwargs):raise ValueError('隔离预览不操作扭矩')
    def prepare_hardware(self,*args,**kwargs):raise ValueError('隔离预览不准备实机')
    def capture_profile(self):raise ValueError('隔离预览不访问串口')
    def scan(self):raise ValueError('隔离预览不访问串口')
