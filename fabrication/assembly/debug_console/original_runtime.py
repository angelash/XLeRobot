"""Guarded upstream Xbox controller, sharing the diagnostic serial owner."""
from __future__ import annotations
import json
import time
import threading
from pathlib import Path
from .control_runtime import ControlRuntime,EXPECTED_SERIALS,ROOT,now,LOG_DIR
from .whole_calibration import WholeCalibration,DESIGN_GEOMETRY,KEYS as RANGE_KEYS,atomic_json
from .limit_selftest import LimitSelfTest
from .control_settings import ControllerSettings,remap_motor_labels,MAPPING_KEYS
from .torque_control import EXPECTED,read_registers,word,write_register,write_limits
from .upstream_teleop import UpstreamTeleop,JOINTS,intentions,neutral,encode_velocity,forward,inverse,wheel_raw

KINEMATIC_PROFILE=ROOT/'kinematic-calibration.json'
SERVICE_PROFILE=ROOT.parents[1]/'.local-deploy'/'hardware-service.json'
CONTROLLER_SETTINGS=ROOT.parents[1]/'.local-deploy'/'controller-settings.json'
# STS3215 12 V supply readings sag briefly while several servos accelerate.
SUPPLY_MIN_V=9.0;SUPPLY_MAX_V=13.5;TEMP_MAX_C=60
SUPPLY_BAD_READS=3
FAULT_RETRY_S=1.0
# Locking a hand-posed joint widens its limits only this far past where it sits.
POSE_MARGIN_RAW=8


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
        self.preset_blocked={};self.joint_faults={}
        self.manual_pose_active=False
        self.supply_bad={};self.stall_marks={};self.resume_on_release=False;self.heartbeat_noticed=False
        self.wheel_pulse_timer=None;self.wheel_pulse_latched=False
        self.settings=ControllerSettings(CONTROLLER_SETTINGS)
        self._settings_from_calibration=False
        self.manual_calibration=WholeCalibration(self,LOG_DIR/'whole-calibration'/'session.json',KINEMATIC_PROFILE)
        self.limit_test=LimitSelfTest(self,LOG_DIR/'limit-selftest',KINEMATIC_PROFILE)

    def arm_ports(self):return self.settings.arm_ports()

    def wheel_ids(self):return self.settings.wheel_ids()

    def update_controller_settings(self,payload):
        with self.io:
            data=self.settings.merged(payload);online=self.controller['controller']=='online'
            changed=[k for k in MAPPING_KEYS if data.get(k)!=self.settings.data.get(k)]
            if online and changed:
                raise ValueError('接管中可以直接改速度、方向和容差；左右臂映射与底盘方向请先结束接管再改')
            old=self.settings.arm_ports();result=self.settings.save(data)
            remap_motor_labels(self.report,self.settings)
            if old!=self.settings.arm_ports() and self.manual_calibration.captures:
                self.manual_calibration.remap_captures(old,self.settings.arm_ports())
            # Live tuning must not rebuild envelopes: the active profile may
            # have widened them to the saved device range.
            if not online:self._make_envelopes();self.load_kinematic_profile()
            self.engine.max_raw=self.settings.data['wheel_max_raw']
            self.log('settings',('接管中实时更新' if online else '控制设置更新')+f'：双臂映射{self.settings.data["arm_mapping"]}',**result)
            return result

    def scan(self):
        super().scan()
        remap_motor_labels(self.report,self.settings)
        if getattr(self,'manual_calibration',None):self.manual_calibration.observe(self.report)

    def _require_not_calibrating(self):
        if getattr(self,'limit_test',None) and self.limit_test.running:
            raise ValueError('限位自检进行中；请等待完成或点“立即停止”')
        if self.manual_calibration.active:raise ValueError('正在手动标定；请保存或结束标定后再准备/接管/开启扭矩')

    def calibration_command(self,action,payload):
        with self.io:
            if isinstance(self,PreviewRuntime):raise ValueError('隔离预览服务不访问实机标定')
            c=self.manual_calibration
            if action=='start':
                if self.limit_test.running:raise ValueError('限位自检进行中，结束后再开始采集')
                return c.start(payload.get('confirmed',False))
            if action=='resume':return c.resume_latest()
            if action=='recover':return c.recover(payload.get('session_id'),payload.get('confirmed',False))
            if action=='merge-checkpoint':return c.merge_checkpoint(payload.get('session_id'),payload.get('confirmed',False))
            if action=='capture':return c.capture(payload.get('step'),payload.get('confirmed_keys'))
            if action=='capture-item':return c.capture_item(payload.get('item'),payload.get('confirmed',False))
            if action=='delete-item':return c.delete_capture(payload.get('item'))
            if action=='clear-captures':return c.clear_captures(payload.get('confirmed',False))
            if action=='rewind':return c.rewind(payload.get('step'))
            if action in ('wheels','base-test'):
                # Chassis directions live in controller settings, so a direction
                # chosen during commissioning also applies to every control mode.
                chassis={k:payload[k] for k in ('base_forward','base_turn') if payload.get(k) is not None}
                if any(self.settings.data.get(k)!=v for k,v in chassis.items()):self.update_controller_settings(chassis)
                if action=='wheels':return c.set_wheels(payload.get('wheels_verified',False),payload.get('confirmed',False))
                return c.base_test(payload.get('kind'),payload.get('confirmed',False))
            if action=='base-confirm':return c.confirm_base(payload.get('forward_observed',False),payload.get('left_observed',False))
            if action=='apply':return c.apply(payload.get('confirmed',False))
            if action=='apply-ranges':return c.apply_ranges(payload.get('confirmed',False))
            if action=='cancel':return c.cancel()
            raise ValueError('未知标定操作')

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
            if self.limit_test.running:raise ValueError('限位自检进行中，不能切换扭矩')
            if enabled:
                self._require_not_calibrating()
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
            self._require_not_calibrating()
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

    # ---- manual posing: release torque, then lock where the user left it ----
    def _pose_selection(self,motors):
        if not motors:return {name:list(ids) for name,ids in EXPECTED.items()}
        selection={}
        for name,ids in motors.items():
            if name not in EXPECTED or not ids or any(int(i) not in EXPECTED[name] for i in ids):raise ValueError('未知电机')
            selection[name]=sorted({int(i) for i in ids})
        return selection

    def _sync_profile_limits(self,changes):
        """Keep a saved range in step with limits widened around a hand pose."""
        if not changes or not KINEMATIC_PROFILE.is_file():return
        data=json.loads(KINEMATIC_PROFILE.read_text(encoding='utf-8'))
        if data.get('serials')!=EXPECTED_SERIALS:return
        joints=data.get('joints',{});touched=False
        for c in changes:
            entry=joints.get(c['key'])
            if not entry or (entry.get('min'),entry.get('max'))!=(c['old_min'],c['old_max']):continue
            entry.update(min=c['min'],max=c['max'])
            entry.setdefault('manual_widen',[]).append({'at':now(),'from':[c['old_min'],c['old_max']],
                                                        'to':[c['min'],c['max']],'position':c['position']})
            touched=True
        if touched:atomic_json(KINEMATIC_PROFILE,data)

    def manual_pose(self,action,motors=None,end_takeover=False):
        """release: torque off so joints move by hand. hold: lock every selected
        joint where it now is. A joint pushed past its stored limits gets those
        limits widened just enough to contain it first (torque still off), so
        the servo never snaps back."""
        if action not in ('release','hold'):raise ValueError('未知手动摆位操作')
        with self.io:
            if isinstance(self,PreviewRuntime):raise ValueError('隔离预览不操作扭矩')
            if self.limit_test.running:raise ValueError('限位自检进行中；请等待完成或点“立即停止”')
            if self.controller['controller']=='online':
                if not end_takeover:raise ValueError('手柄接管中；结束接管后再手动摆位')
                self.stop()
            selection=self._pose_selection(motors)
            if action=='release':
                result=self.torque(False,selection)
                self.manual_pose_active=True
                self.log('manual_pose',f'手动摆位：已卸力{result["count"]}个电机，可用手调整姿势',motors=result['motors'])
                return {**result,'action':'release','widened':[],'skipped':[]}
            self._require_not_calibrating()
            self._make_envelopes()
            skipped=[];widened=[]
            for name,ids in list(selection.items()):
                for i in list(ids):
                    reason=self.service_blocked.get(f'{name}:{i}')
                    if reason:skipped.append({'port':name,'id':i,'reason':reason});ids.remove(i)
                if not ids:selection.pop(name)
            if not selection:raise ValueError('所选电机都在检修中，未锁定')
            self.session.open()
            for name,ids in selection.items():
                p=self.session.ports[name]
                for i in ids:
                    if name=='COM4' and i in (9,10):continue
                    reg=read_registers(p,i)
                    if reg[33]!=0:continue
                    pos,low,high=word(reg,56),word(reg,9),word(reg,11)
                    if low<=pos<=high:continue
                    if reg[40]:raise ValueError(f'{name}:{i}在扭矩开启时位于限位外，未修改')
                    new_low,new_high=min(low,max(0,pos-POSE_MARGIN_RAW)),max(high,min(4095,pos+POSE_MARGIN_RAW))
                    write_limits(p,i,new_low,new_high)
                    widened.append({'key':f'{name}:{i}','port':name,'id':i,'position':pos,
                                    'old_min':low,'old_max':high,'min':new_low,'max':new_high})
            self._sync_profile_limits(widened)
            result=self.torque(True,selection)
            self.manual_pose_active=False
            self.load_kinematic_profile()
            message=f'手动摆位：已锁定{result["count"]}个电机在当前位置'
            if widened:message+=f'；{len(widened)}个关节超出原限位，已放宽到包住当前位置'
            self.log('manual_pose',message,motors=result['motors'],widened=widened,skipped=skipped)
            return {**result,'action':'hold','widened':widened,'skipped':skipped,'message':message}

    def begin(self,confirmed):
        with self.io:
            self._require_not_calibrating()
            if self.controller['controller']=='online' and self.control_profile!='diagnostic':
                raise ValueError('先由你结束原版接管，再切换单关节调试')
            if self.control_profile!='diagnostic':self._hold()
            self.control_profile='diagnostic'
            return super().begin(confirmed)

    def capture_profile(self):
        result=super().capture_profile()
        self.load_kinematic_profile()
        return result

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
        if self.control_profile not in ('original_hardware','original_joint','original_preset'):
            return super().check_watchdog()
        if self.controller['controller']!='online' or self.controller['paused']:return
        # The native gamepad is the operator's dead-man input. A hidden or
        # closed web page is reported, but it does not stop gamepad control.
        if time.monotonic()-self.pad_time>.5:
            self._pause('手柄输入超过500ms未更新；手柄恢复并回中后自动继续');return
        if time.monotonic()-self.ui_heartbeat>3:
            if not self.heartbeat_noticed:
                self.heartbeat_noticed=True
                self.original_notice('heartbeat','网页已关闭或在后台；手柄仍可控制，View结束接管')
        else:self.heartbeat_noticed=False
        if self.feedback_window:self._check_feedback()

    def _supply_check(self,key,r):
        """Fault only on a reading that stays out of range, not a brief sag."""
        volts=r[62]/10;temp=r[63]
        if SUPPLY_MIN_V<=volts<=SUPPLY_MAX_V and temp<TEMP_MAX_C:
            self.supply_bad.pop(key,None);return
        count=self.supply_bad.get(key,0)+1;self.supply_bad[key]=count
        if count>=SUPPLY_BAD_READS:
            self.supply_bad.pop(key,None)
            raise ValueError(f'供电/温度持续异常（{volts:.1f}V，{temp}°C）')
        self.log('supply_sag',f'{key}瞬时读数{volts:.1f}V {temp}°C，继续观察',key=key,volts=volts,temp=temp)

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

    def readiness(self,profile=None):
        profile=profile or self.control_profile
        channels={};motors=self.motors()
        for side,port in self.arm_ports().items():
            arm_channels=([('lift',[2],False),('elbow',[3],False)] if profile=='original_joint'
                          else [('position',[2,3,4],True)])
            for channel,ids,needs_ref in arm_channels+[('pan',[1],False),('pitch',[4],False),('roll',[5],False),('grip',[6],True)]:
                reasons=[]
                for i in ids:
                    m=motors.get((port,i));e=self.envelopes.get((port,i))
                    if not m or not e:reasons.append(f'{port}:{i}未读取');continue
                    reasons.extend(self.joint_readiness_reasons(m,e))
                    if f'{port}:{i}' in self.service_blocked:reasons.append(self.service_blocked[f'{port}:{i}'])
                    if needs_ref and profile!='original_joint' and not self.reference(port,i):reasons.append(f'{m["label"]}运动学零位/方向未确认')
                channels[f'{side}_{channel}']={'ready':not reasons,'reasons':reasons}
        for i in (7,8):
            m=motors.get(('COM3',i));e=self.envelopes.get(('COM3',i),{})
            reasons=[]
            if not m:reasons.append(f'COM3:{i}未读取')
            else:reasons.extend(self.joint_readiness_reasons(m,e))
            channels[f'head_{i}']={'ready':not reasons,'reasons':reasons}
        reasons=[]
        # Base geometry uses the published 127 mm / 250 mm values and the
        # user's forward/turn settings; field confirmation is optional.
        for i in (9,10):
            m=motors.get(('COM4',i))
            if not m or m['mode']!=1 or not m['torque']:reasons.append(f'COM4:{i}需要速度模式且扭矩开启（不自动修改）')
        channels['base']={'ready':not reasons,'reasons':reasons}
        return channels

    def preset_readiness(self):
        """Per-joint availability for direct preset control; torque is not
        a reason because starting the preset holds the current position."""
        motors=self.motors();channels={}
        def check(port,i):
            m=motors.get((port,i));key=f'{port}:{i}'
            if not m:return {'ready':False,'reasons':[f'{port}:{i}未读取'],'note':None,'port':port,'id':i}
            reasons=[];note=None;span=m['max']-m['min']
            if key in self.service_blocked:reasons.append(self.service_blocked[key])
            if m['mode']!=0:reasons.append(f'当前模式{m["mode"]}，需要位置模式0')
            if span<24:reasons.append(f'设备限位只有{max(span,0)}计数；在整机标定里点“保存已采行程”后放开')
            elif not m['min']<=m['position']<=m['max']:
                reasons.append(f'当前位置{m["position"]}不在设备限位{m["min"]}–{m["max"]}内；在整机标定里点“保存已采行程”后放开')
            if not SUPPLY_MIN_V<=m['voltage']<=SUPPLY_MAX_V or m['temperature']>=TEMP_MAX_C:reasons.append(f'供电/温度异常（{m["voltage"]}V，{m["temperature"]}°C）')
            if key in self.joint_faults:reasons.append(self.joint_faults[key]['reason']+'（约1秒后自动重试）')
            if not reasons and span<160:
                note=f'设备限位只有{span*360/4096:.1f}°，仍是旧试控窗口；保存已采行程后放开'
            elif not reasons:note=f'可动范围 {span*360/4096:.0f}°'
            return {'ready':not reasons,'reasons':reasons,'note':note,'port':port,'id':i}
        names={1:'pan',2:'lift',3:'elbow',4:'pitch',5:'roll',6:'grip'}
        for side,port in self.arm_ports().items():
            for i,name in names.items():channels[f'{side}_{name}']=check(port,i)
        for i in (7,8):channels[f'head_{i}']=check('COM3',i)
        reasons=[]
        for i in (9,10):
            m=motors.get(('COM4',i))
            if not m:reasons.append(f'COM4:{i}未读取')
            elif m['mode']!=1:reasons.append(f'COM4:{i}不是速度模式1')
        if 'base' in self.joint_faults:reasons.append(self.joint_faults['base']['reason']+'（约1秒后自动重试）')
        forward,turn=self.settings.base_signs()
        channels['base']={'ready':not reasons,'reasons':reasons,
            'note':'轮径127mm/轮距250mm为资料值；前进'+('已反向' if forward<0 else '按方案A')+'，转向'+('已反向' if turn<0 else '正常')}
        return channels

    def _wheel_geometry(self):
        wheels=self.kinematic_profile.get('wheels',{})
        try:radius=float(wheels.get('radius_m',DESIGN_GEOMETRY['radius_m']));track=float(wheels.get('track_m',DESIGN_GEOMETRY['track_m']))
        except (TypeError,ValueError):radius,track=DESIGN_GEOMETRY['radius_m'],DESIGN_GEOMETRY['track_m']
        if not (.02<=radius<=.2 and .1<=track<=1):radius,track=DESIGN_GEOMETRY['radius_m'],DESIGN_GEOMETRY['track_m']
        return radius,track

    def saved_ranges(self):
        """Joints whose servo limits currently equal a saved observed range."""
        joints=self.kinematic_profile.get('joints',{});motors=self.motors();saved=[]
        for key in RANGE_KEYS:
            entry=joints.get(key) or {};port,i=key.split(':');m=motors.get((port,int(i)))
            if m and (entry.get('range_verified') or entry.get('full_range_confirmed')) and all(entry.get(k)==m[k] for k in ('min','max','homing_raw')):
                saved.append(key)
        return saved

    def wheel_command(self,v,omega,max_raw=None):
        """Upstream differential drive plus the user's direction settings.

        Returns {motor_id: raw velocity}. Forward flips translation only,
        turn flips rotation only; the left wheel keeps the upstream mirror.
        """
        forward_sign,turn_sign=self.settings.base_signs();radius,track=self._wheel_geometry()
        raw=wheel_raw(forward_sign*v,turn_sign*omega,radius,track,max_raw or self.settings.data['wheel_max_raw'])
        ids=self.wheel_ids()
        return {ids['left']:raw[0],ids['right']:raw[1]}

    def _zero_wheels(self):
        failures=[]
        for i in (9,10):
            try:write_register(self.session.ports['COM4'],i,46,0,2)
            except Exception as exc:failures.append(f'轮{i} '+str(exc))
        self.engine.wheels=[0,0];self.wheel_goals=[0,0]
        if failures:raise RuntimeError('；'.join(failures))
        self.wheels_commanded=False

    def _drive_base(self,v,omega):
        if not (v or omega):
            if self.wheels_commanded:self._zero_wheels()
            return
        command=self.wheel_command(v,omega)
        if self.wheels_commanded and self.wheel_goals==[command[9],command[10]]:return
        p=self.session.ports['COM4']
        for i in (9,10):
            r=read_registers(p,i)
            if r[33]!=1 or r[40]!=1:raise ValueError('轮子模式/扭矩发生变化')
            self._supply_check(f'COM4:{i}',r)
        self.wheels_commanded=True
        for i in (9,10):write_register(p,i,46,encode_velocity(command[i]),2)
        self.wheel_goals=[command[9],command[10]];self.engine.wheels=list(self.wheel_goals)

    def whole_robot_setup(self):
        manual=self.manual_calibration.snapshot()
        if manual['active']:
            checks=[]
            by_item={x['item']:x for x in manual['apply_blockers'] if 'item' in x}
            for side,label in [('left','左臂联动与夹爪'),('right','右臂联动与夹爪')]:
                reasons=[by_item[side+'_'+kind]['message'] for kind in ('a','b','c','open','closed') if side+'_'+kind in by_item]
                checks.append({'key':side,'label':label,'ready':not reasons,'reasons':reasons})
            checks.append({'key':'base','label':'底盘（资料几何，方向可在设置里切换）',
                           'ready':True,'optional':True,'reasons':[]})
            reasons=[x['message'] for x in manual['apply_blockers'] if 'item' not in x]
            checks.append({'key':'range','label':'完整操作行程','ready':not reasons,'reasons':reasons})
            return {'ready':manual['apply_ready'],'checks':checks,'live_manual':True}
        channels=self.readiness('original_hardware')
        checks=[]
        ports=self.arm_ports()
        for side,label,keys in [('left','左臂联动与夹爪',['position','pan','pitch','roll','grip']),
                                ('right','右臂联动与夹爪',['position','pan','pitch','roll','grip'])]:
            reasons=[]
            for key in keys:
                reasons.extend(r for r in channels[side+'_'+key]['reasons'] if not r.endswith('扭矩关闭'))
            checks.append({'key':side,'label':label,'ready':not reasons,'reasons':list(dict.fromkeys(reasons))})
        reasons=[]
        for i in (9,10):
            motor=self.motors().get(('COM4',i))
            if not motor:reasons.append(f'COM4:{i}未读取')
            elif motor.get('mode')!=1:reasons.append(f'COM4:{i}未处于速度模式')
        checks.append({'key':'base','label':'底盘（资料几何，方向可在设置里切换）','ready':not reasons,'optional':True,'reasons':reasons})
        full_range=self.kinematic_profile.get('full_manual_range_verified') is True
        checks.append({'key':'range','label':'完整操作行程','ready':full_range,
                       'reasons':[] if full_range else ['当前保存的是局部测试窗口；需人工逐关节采集真实可用行程']})
        return {'ready':all(c['ready'] for c in checks if not c.get('optional')),'checks':checks}

    def begin_whole_robot(self,confirmed):
        """The normal real-robot entry must never report a partial takeover."""
        with self.io:
            self._require_not_calibrating()
            if isinstance(self,PreviewRuntime):raise ValueError('隔离预览服务不能控制实机')
            if not confirmed:raise ValueError('请确认整机遥操作与周围安全')
            if self.controller['controller']=='online':raise ValueError('先结束当前接管，再开启整机遥操作')
            if time.monotonic()-self.pad_time>.5 or not neutral(self.gamepad):
                raise ValueError('请连接手柄并将全部输入回中松开')
            self.capture_profile();self.load_kinematic_profile()
            setup=self.whole_robot_setup()
            if not setup['ready']:
                missing=[c['label']+'：'+'；'.join(c['reasons']) for c in setup['checks'] if not c['ready']]
                raise ValueError('整机初始化未完成。'+' | '.join(missing))
            for side,port in self.arm_ports().items():
                angles=[]
                for i in (2,3):
                    ref=self.reference(port,i);raw=self.motors()[(port,i)]['position']
                    angles.append(ref['reference_deg']+(raw-ref['reference_raw'])*ref['direction']*360/4096)
                try:
                    solved=inverse(*forward(*angles))
                    if max(abs(a-b) for a,b in zip(angles,solved))>2:raise ValueError('不在上游逆解分支')
                except ValueError as exc:
                    label='左臂' if side=='left' else '右臂'
                    raise ValueError(label+'当前姿态超出末端联动分支；请在支撑下回到标定姿态B附近再开启') from exc
            prepared=self.prepare_hardware(True)
            if prepared['blocked']:raise ValueError('整机准备仍有未通过项，请查看初始化清单')
            return self.begin_original('hardware',True,full_range=True)

    def begin_trial(self,confirmed):
        """Start bounded joint trials without requiring an IK/full-range file."""
        with self.io:
            self._require_not_calibrating()
            if isinstance(self,PreviewRuntime):raise ValueError('隔离预览服务不能准备实机试控')
            if not confirmed:raise ValueError('请确认手臂有支撑、周围有小幅动作空间')
            if self.controller['controller']=='online':raise ValueError('先结束当前接管，再开启低速试控')
            if time.monotonic()-self.pad_time>.5 or not neutral(self.gamepad):
                raise ValueError('请连接手柄并将全部输入回中松开')
            self.capture_profile();missing=[]
            for port in self.report['ports']:
                name=port['port']
                for motor in port['motors']:
                    i=motor['id'];key=f'{name}:{i}';wheel=name=='COM4' and i in (9,10)
                    if key in self.service_blocked:missing.append(motor['label']+'：'+self.service_blocked[key]);continue
                    if wheel:
                        if motor['mode']!=1:missing.append(motor['label']+'需要速度模式')
                    elif (motor['mode']!=0 or not self.envelopes[(name,i)]['available'] or
                          not motor['min']<=motor['position']<=motor['max']):
                        missing.append(motor['label']+'需要有效的当前位置试控窗口')
                    if not 10<=motor['voltage']<=13 or motor['temperature']>=50:
                        missing.append(motor['label']+'供电/温度未就绪')
            if missing:raise ValueError('低速试控尚未就绪：'+'；'.join(missing))
            prepared=self.prepare_hardware(True)
            if len(prepared['ready'])!=16 or prepared['blocked']:raise ValueError('未完整准备16个部件，请查看准备结果')
            return self.begin_original('joint',True)

    def begin_preset(self,confirmed):
        """Open the upstream stick layout on every usable joint immediately.

        Direct relative axes inside each servo's saved limit; no reference
        pose is required. A joint that is not usable is listed and skipped,
        it never blocks the rest of the robot.
        """
        with self.io:
            self._require_not_calibrating()
            if isinstance(self,PreviewRuntime):raise ValueError('隔离预览不能驱动实机')
            if not confirmed:raise ValueError('请明确确认预设直控试运行与周围安全')
            if self.controller['controller']=='online':raise ValueError('先结束当前接管')
            if time.monotonic()-self.pad_time>.5 or not neutral(self.gamepad):
                raise ValueError('请连接手柄并全部回中')
            self.capture_profile();self._make_envelopes();self.joint_faults={};self.supply_bad={}
            self.resume_on_release=False;self.heartbeat_noticed=False
            readiness=self.preset_readiness()
            if not any(v['ready'] for k,v in readiness.items() if k!='base'):
                raise ValueError('没有可直接控制的关节：'+'；'.join(f'{self.motors().get((v["port"],v["id"]),{}).get("label",k)}：{"；".join(v["reasons"])}' for k,v in readiness.items() if k!='base'))
            prepared=self.prepare_hardware(True)
            motors=self.motors()
            self.preset_blocked={f'{x["port"]}:{x["id"]}':x['reason'] for x in prepared['blocked']}
            for k,v in readiness.items():
                if k!='base' and not v['ready']:self.preset_blocked.setdefault(f'{v["port"]}:{v["id"]}','；'.join(v['reasons']))
            if not readiness['base']['ready']:
                for i in (9,10):self.preset_blocked.setdefault(f'COM4:{i}','；'.join(readiness['base']['reasons']))
            if not any(k for k in (f'{x["port"]}:{x["id"]}' for x in prepared['ready']) if k not in ('COM4:9','COM4:10')):
                raise ValueError('没有关节完成保持当前位置：'+'；'.join(f'{k} {v}' for k,v in self.preset_blocked.items()))
            self.control_profile='original_preset'
            radius,track=self._wheel_geometry()
            # The engine only supplies speed level and the preview; arm joints
            # are driven directly, so its virtual IK bounds are not enforced.
            self.engine=UpstreamTeleop(None,radius,track,self.settings.data['wheel_max_raw']);self.engine.arm_ports=self.arm_ports()
            for e in self.envelopes.values():e['min']=e['device_min'];e['max']=e['device_max']
            self.tick_time=time.monotonic();self.ui_heartbeat=self.tick_time
            self.direct_targets={};self.feedback_window={};self.moving={}
            self.grip_touched={'left':False,'right':False};self.last_menu=False
            self.notice=None;self.last_command=None;self.wheel_goals=[0,0]
            skipped=[f'{motors.get(tuple([k.split(":")[0],int(k.split(":")[1])]),{}).get("label",k)}' for k in self.preset_blocked]
            self.controller.update(controller='online',paused=False,pause_reason=None,armed=True,error=None,
                last_action='预设直控已接管：拨杆直接驱动关节'+('；未接入：'+'、'.join(skipped) if skipped else '；全部部件已接入'),updated_at=now())
            self.log('controller_start',self.controller['last_action'],profile=self.control_profile,blocked=self.preset_blocked)
            if skipped:self.original_notice('preset_skipped','以下部件未接入，其余可直接控制：'+'；'.join(f'{n}：{r}' for n,r in zip(skipped,self.preset_blocked.values())))
            return self.controller.copy()

    def begin_original(self,target,confirmed,full_range=False):
        with self.io:
            self._require_not_calibrating()
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
                elif full_range:
                    if not self.whole_robot_setup()['ready']:raise ValueError('完整行程和整机通道未确认')
                    for e in self.envelopes.values():
                        e['min']=e['device_min'];e['max']=e['device_max']
            self.control_profile='original_'+target
            angles=None
            if target=='hardware':
                angles={s:dict(zip(JOINTS,[0,0,2,-2,0,90])) for s in ('left','right')}
                for side,port in self.arm_ports().items():
                    for i,j in enumerate(JOINTS,1):
                        ref=self.reference(port,i)
                        if ref:
                            raw=self.motors()[(port,i)]['position']
                            angles[side][j]=(2+(raw-ref['closed_raw'])*88/(ref['open_raw']-ref['closed_raw'])) if i==6 else ref['reference_deg']+(raw-ref['reference_raw'])*ref['direction']*360/4096
            radius,track=self._wheel_geometry()
            self.engine=UpstreamTeleop(angles,radius,track,self.settings.data['wheel_max_raw'])
            self.engine.arm_ports=self.arm_ports()
            if target=='hardware':
                for side,port in self.arm_ports().items():
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
        if self.control_profile in ('original_hardware','original_joint','original_preset'):
            for (port,i) in list(self.moving):
                try:
                    self._hold_one((port,i))
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
            if self.control_profile in ('original_hardware','original_joint','original_preset'):self.scan()
            self._hold();self.ui_heartbeat=time.monotonic();self.tick_time=self.ui_heartbeat
            if self.control_profile=='original_hardware':
                for side,port in self.arm_ports().items():
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
            if not raw.get('connected'):self._pause('手柄断开；重新连接并回中后自动继续');return
            intent=intentions(raw)
            if intent['stop']:self._stop('View结束接管，保持姿态；不执行原脚本归零');return
            menu=intent['pause'];edge=menu and not self.last_menu;self.last_menu=menu
            if edge and not self.controller['paused']:self._pause('Menu暂停所有动作；再按一次Menu继续');return
            if self.controller['paused']:
                # Menu toggles any pause; a lost/stale gamepad resumes by itself
                # once it reports neutral input again. Old input is never replayed.
                if edge:self.resume_on_release=True
                reason=self.controller.get('pause_reason') or ''
                if (self.resume_on_release or reason.startswith('手柄')) and neutral(raw):
                    self.resume_on_release=False
                    try:self.resume()
                    except (ValueError,RuntimeError) as exc:self.original_notice('resume','暂未恢复：'+str(exc))
                return
            stamp=time.monotonic();dt=min(.1,max(0,stamp-self.tick_time))
            if dt<.04:return
            self.tick_time=stamp
            self.check_watchdog()
            if self.controller['paused']:return
            previous={s:dict(q) for s,q in self.engine.q.items()}
            if self.control_profile!='original_joint':self.engine.step(raw,dt)
            else:self.engine.last_input=intent
            # Preset drives joints directly; virtual IK limits are not physical.
            if self.engine.blockers and self.control_profile!='original_preset':self.original_notice('limit','；'.join(self.engine.blockers))
            if self.control_profile=='original_joint':self._execute_joint(intent,dt)
            elif self.control_profile=='original_preset':self._execute_preset(intent,dt)
            elif self.control_profile=='original_hardware':
                for side in ('left','right'):
                    if not self.grip_touched[side] and intent[side]['trigger']<=.75:
                        self.engine.q[side]['grip']=previous[side]['grip']
                self._execute(intent,previous,dt)
            self.controller.update(updated_at=now(),last_action='原版Xbox · '+self.control_profile.split('_')[-1]+' · 左'+intent['left']['mode']+' / 右'+intent['right']['mode'])

    def _execute_preset(self,intent,dt):
        """Direct axes with upstream stick semantics; faults stay per joint."""
        for side,port in self.arm_ports().items():
            item=intent[side];mode=item['mode']
            for i,channel in ((1,'pan'),(2,'lift'),(3,'elbow'),(4,'pitch'),(5,'roll')):
                active=(mode=='pan' and i==1) or (mode=='position' and i in (2,3)) or (mode=='wrist' and i in (4,5))
                horizontal=item['h'] if i in (1,2,5) else -item['v']
                axis=self.settings.axis(port,i)
                rate=horizontal*axis['joint_rate_deg_s']*axis['direction'] if active else 0
                self._preset_joint(port,i,rate,dt)
            if item['trigger']>.75:self.grip_touched[side]=True
            if self.grip_touched[side]:
                axis=self.settings.axis(port,6)
                # Gripper speed is configured in raw/s; _direct expects deg/s.
                rate=axis['grip_raw_s']*360/4096*axis['direction']
                self._preset_joint(port,6,rate if item['trigger']>.75 else -rate,dt)
        for i,name in ((7,'pan'),(8,'tilt')):
            axis=self.settings.axis('COM3',i)
            self._preset_joint('COM3',i,intent['head'][name]*axis['joint_rate_deg_s']*axis['direction'],dt)
        level=self.engine.speed_level;v=intent['base']['v']*level;omega=intent['base']['omega']*level
        fault=self.joint_faults.get('base')
        if fault and time.monotonic()-fault['at']>=FAULT_RETRY_S:
            self.joint_faults.pop('base',None);fault=None;self.log('joint_retry','底盘自动重试')
        if fault or 'COM4:9' in self.preset_blocked or 'COM4:10' in self.preset_blocked:
            if v or omega:self.original_notice('blocked','底盘未接入：'+((fault or {}).get('reason') or self.preset_blocked.get('COM4:9') or self.preset_blocked.get('COM4:10')))
            return
        try:self._drive_base(v,omega)
        except ValueError as exc:
            self.joint_faults['base']={'reason':str(exc),'at':time.monotonic()}
            self._zero_wheels()
            self.original_notice('joint_fault','底盘暂停：'+str(exc)+'；约1秒后自动重试，双臂继续可控')

    def _preset_joint(self,port,i,rate,dt):
        key=f'{port}:{i}';label=self.motors().get((port,i),{}).get('label',key)
        fault=self.joint_faults.get(key)
        if fault and time.monotonic()-fault['at']>=FAULT_RETRY_S:
            # Transient faults (supply sag, a lost ACK) clear by themselves.
            self.joint_faults.pop(key,None);fault=None
            self.log('joint_retry',label+'自动重试',port=port,id=i)
        reason=(fault or {}).get('reason') or self.preset_blocked.get(key)
        if reason:
            if rate:self.original_notice('blocked',label+'未接入：'+reason)
            return
        if not rate:
            if (port,i) in self.moving or (port,i) in self.direct_targets:
                try:self._direct(port,i,0,dt)
                except ValueError as exc:self._joint_fault(port,i,label,exc)
            return
        try:self._direct(port,i,rate,dt)
        except ValueError as exc:self._joint_fault(port,i,label,exc)

    def _joint_fault(self,port,i,label,exc):
        """Hold one joint after a refusal; it is retried automatically."""
        self.joint_faults[f'{port}:{i}']={'reason':str(exc),'at':time.monotonic()}
        try:
            if (port,i) in self.moving:self._hold_one((port,i))
        except ValueError:pass
        for table in (self.moving,self.direct_targets,self.feedback_window):table.pop((port,i),None)
        self.original_notice('joint_fault',label+'暂停：'+str(exc)+'；约1秒后自动重试，其余部件继续可控')

    def _channel(self,key):
        value=self.readiness()[key]
        if not value['ready']:self.original_notice('blocked',key+'：'+'；'.join(value['reasons']))
        return value['ready']

    def _execute_joint(self,intent,dt):
        """A clearly separate raw-joint mode; no guessed IK references."""
        for side,port in self.arm_ports().items():
            item=intent[side];mode=item['mode']
            rate_max=self.settings.data['joint_rate_deg_s']
            rates={1:item['h']*rate_max if mode=='pan' else 0,
                   2:item['h']*rate_max if mode=='position' else 0,
                   3:-item['v']*rate_max if mode=='position' else 0,
                   4:-item['v']*rate_max if mode=='wrist' else 0,
                   5:item['h']*rate_max if mode=='wrist' else 0}
            # Triggers exercise only this session's bounded grip interval.
            if item['trigger']>.75:self.grip_touched[side]=True
            if self.grip_touched[side]:
                e=self.envelopes.get((port,6),{})
                motor=self.motors().get((port,6),{})
                if e.get('available') and motor.get('torque') and motor.get('mode')==0:
                    rate=self.settings.data['grip_raw_s']*360/4096
                    self._direct(port,6,rate if item['trigger']>.75 else -rate,dt)
            for i,rate in rates.items():
                e=self.envelopes.get((port,i),{});m=self.motors().get((port,i),{})
                if rate and (f'{port}:{i}' in self.service_blocked or not e.get('available') or not m.get('torque') or m.get('mode')!=0):
                    if (port,i) in self.moving:self._hold_one((port,i))
                    reason=self.service_blocked.get(f'{port}:{i}') or e.get('reason') or '请先准备实机并核对模式/扭矩'
                    self.original_notice('blocked',m.get('label',f'{port}:{i}')+'：'+reason);continue
                self._direct(port,i,rate,dt)
        for i,name in [(7,'pan'),(8,'tilt')]:
            axis=self.settings.axis('COM3',i)
            rate=intent['head'][name]*self.settings.data['joint_rate_deg_s']*axis['direction'];e=self.envelopes.get(('COM3',i),{});m=self.motors().get(('COM3',i),{})
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
            command=self.wheel_command(intent['base']['v'],intent['base']['omega'],max_raw=160)
            scale=160/max(1,max(abs(v) for v in command.values()))
            command={i:round(v*scale) for i,v in command.items()};raw=[command[9],command[10]]
            self.wheel_pulse_timer=threading.Timer(.18,self._stop_wheel_pulse)
            self.wheel_pulse_timer.daemon=True;self.wheel_pulse_timer.start()
            for i,value in command.items():
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
        released=r[40]==0
        if not released:
            if r[33]!=0 or not word(r,9)<=pos<=word(r,11):
                raise ValueError(f'{port}:{i}当前位置/模式不能确认保持，未重发目标')
            write_register(self.session.ports[port],i,42,pos,2)
        message=f'{port}:{i}扭矩已关闭，保留关闭状态' if released else f'{port}:{i}回中保持，实测{pos}'
        self.log('torque_off' if released else 'feedback',message,port=port,id=i,position=pos,target=expected,moved=abs(pos-origin)>=1)
        if self.last_command and self.last_command.get('port')==port and self.last_command.get('id')==i:
            self.last_command.update(position=pos,outcome='torque_off' if released else 'feedback' if abs(pos-origin)>=1 else 'interrupted',moved=abs(pos-origin)>=1,reached=abs(pos-expected)<=(16 if self.control_profile=='original_joint' else 6))
        self.moving.pop(key,None);self.feedback_window.pop(key,None)

    def _goal(self,port,i,value):
        if f'{port}:{i}' in self.service_blocked:raise ValueError(self.service_blocked[f'{port}:{i}'])
        e=self.envelopes[(port,i)];target=round(max(e['min'],min(e['max'],value)))
        if value<e['min'] or value>e['max']:
            self.direct_targets[(port,i)]=float(target)
            # A gripper resting at its open end is the normal released state.
            if i!=6:self.original_notice('limit',e['label']+'到达限位，可反向；接管保留')
        key=(port,i);p=self.session.ports[port];r=read_registers(p,i);pos=word(r,56)
        if r[33]!=0 or r[40]!=1:raise ValueError(e['label']+'模式/扭矩变化，未自动修改')
        self._supply_check(f'{port}:{i}',r)
        axis=self.settings.axis(port,i)
        if abs(target-pos)>axis['follow_delta_raw']:
            self.original_notice('blocked',e['label']+'目标跟随滞后，已停止累积')
            # Keep the last bounded goal while awaiting feedback. Resetting
            # to the encoder on every lag would repeatedly create tiny goals
            # and could hide a real stall inside the trial tolerance.
            self.direct_targets[key]=float(self.moving.get(key,pos));return False
        if target==pos or self.moving.get(key)==target:return True
        if key not in self.moving:write_register(p,i,42,pos,2)
        # Track before the final write: a missing ACK can still mean the servo
        # accepted the goal, so the fault handler must attempt a hold here.
        self.moving[key]=target
        speed=axis['grip_raw_s'] if i==6 else axis['joint_rate_deg_s']*4096/360
        write_register(p,i,46,min(1000,max(60,int(speed))),2)
        write_register(p,i,42,target,2)
        self.feedback_window.setdefault(key,{'origin':pos,'at':time.monotonic()})
        self.last_command={'port':port,'id':i,'label':e['label'],'origin':pos,'target':target,'outcome':'pending','time':now()}
        self.log('command',e['label']+f'：{pos} → {target}',port=port,id=i,origin=pos,target=target,profile=self.control_profile)
        return True

    def _execute(self,intent,previous,dt):
        for side,port in self.arm_ports().items():
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
                    if all(self.envelopes[(port,i)]['min']<=raw<=self.envelopes[(port,i)]['max'] and abs(raw-self.motors()[(port,i)]['position'])<=self.settings.data['follow_delta_raw'] for i,raw in targets):
                        for i,raw in targets:self._goal(port,i,raw)
                    else:
                        self.engine.q[side]=previous[side]
                        self.original_notice('limit',side+'末端步进被关节限位阻挡；可反向，接管保留')
                else:self.engine.q[side]=previous[side]
            else:
                for i in (2,3):
                    if (port,i) in self.moving:self._hold_one((port,i))
            # Direct pan / wrist channels use relative raw increments, no assumed zero.
            rate_max=self.settings.data['joint_rate_deg_s']
            for i,channel,rate in [(1,'pan',item['h']*rate_max if mode=='pan' else 0),
                                   (4,'pitch',-item['v']*rate_max if mode=='wrist' else 0),
                                   (5,'roll',item['h']*rate_max if mode=='wrist' else 0)]:
                if i==4 and mode=='position' and moving:continue
                if rate and not self._channel(side+'_'+channel):
                    if (port,i) in self.moving:self._hold_one((port,i))
                    continue
                ref=self.reference(port,i)
                self._direct(port,i,rate*(ref['direction'] if ref and i==4 else 1),dt)
                if ref and (port,i) in self.direct_targets:
                    self.engine.q[side][JOINTS[i-1]]=ref['reference_deg']+(self.direct_targets[(port,i)]-ref['reference_raw'])*ref['direction']*360/4096
                    if i==4 and mode=='wrist':
                        q=self.engine.q[side];self.engine.pitch[side]=q['lift']+q['elbow']+q['wrist']
            if item['trigger']>.75:self.grip_touched[side]=True
            if self.grip_touched[side]:
                if self._channel(side+'_grip'):
                    ref=self.reference(port,6)
                    raw=ref['closed_raw']+(self.engine.q[side]['grip']-2)/88*(ref['open_raw']-ref['closed_raw'])
                    origin=self.moving.get((port,6),self.motors()[(port,6)]['position'])
                    limit=self.settings.data['grip_raw_s']*dt
                    raw=origin+max(-limit,min(limit,raw-origin))
                    accepted=self._goal(port,6,raw)
                    actual=raw if accepted else origin
                    self.engine.q[side]['grip']=2+(actual-ref['closed_raw'])*88/(ref['open_raw']-ref['closed_raw'])
                else:self.engine.q[side]['grip']=previous[side]['grip']
        for i,axis_name in [(7,'pan'),(8,'tilt')]:
            axis=self.settings.axis('COM3',i)
            rate=intent['head'][axis_name]*axis['joint_rate_deg_s']*axis['direction']
            if rate and not self._channel(f'head_{i}'):
                if ('COM3',i) in self.moving:self._hold_one(('COM3',i))
                continue
            self._direct('COM3',i,rate,dt)
        level=self.engine.speed_level;v=intent['base']['v']*level;omega=intent['base']['omega']*level
        if v or omega:
            if not self._channel('base'):
                if self.wheels_commanded:
                    for i in (9,10):write_register(self.session.ports['COM4'],i,46,0,2)
                    self.wheels_commanded=False;self.wheel_goals=[0,0]
                self.engine.wheels=[0,0];return
        self._drive_base(v,omega)

    def _check_feedback(self):
        if self.control_profile=='diagnostic':return super()._check_feedback()
        return self._check_original_feedback()

    def _stall(self,key,pos,expected):
        """An obstructed joint stops alone; the takeover keeps running.

        The stop point is kept as limit evidence for the self-test page.
        A gripper closing on an object keeps a light, bounded squeeze.
        """
        port,i=key;e=self.envelopes.get(key,{});label=e.get('label',f'{port}:{i}')
        direction=1 if expected>pos else -1;limit=e.get('max') if direction>0 else e.get('min')
        self.stall_marks[f'{port}:{i}']={'position':pos,'target':expected,'direction':direction,
                                         'soft_limit':limit,'time':now()}
        self.feedback_window.pop(key,None)
        try:
            if i==6:
                squeeze=pos+max(-40,min(40,expected-pos))
                write_register(self.session.ports[port],i,42,squeeze,2)
                self.moving[key]=squeeze;self.direct_targets[key]=float(squeeze)
                self.original_notice('grip',label+'已夹住或受阻，保持轻夹；松开扳机张开')
            else:
                if key in self.moving:self._hold_one(key)
                self.direct_targets[key]=float(pos)
                gap=abs(limit-pos) if limit is not None else None
                self.original_notice('stall',f'{label}在{pos}受阻'+(f'（距限位{gap}计数）' if gap is not None else '')+'，已停住；可反向')
        except ValueError as exc:
            self._joint_fault(port,i,label,exc)
        self.log('stall',f'{label}受阻：目标{expected}，实测{pos}',port=port,id=i,position=pos,target=expected,soft_limit=limit)

    def _check_original_feedback(self):
        if self.control_profile not in ('original_hardware','original_joint','original_preset'):return
        for key,window in list(self.feedback_window.items()):
            if time.monotonic()-window['at']<.8:continue
            port,i=key;pos=word(read_registers(self.session.ports[port],i),56)
            expected=self.moving.get(key,pos);delta=abs(pos-window['origin'])
            reached=abs(pos-expected)<=(16 if self.control_profile=='original_joint' else 6)
            if not reached and abs(expected-window['origin'])>=4 and delta<2:
                self._stall(key,pos,expected);continue
            self.log('feedback',f'{port}:{i}编码器变化{delta}计数'+('，进入目标容差' if reached else ''),
                     port=port,id=i,position=pos,target=expected,origin=window['origin'],moved=delta>=2,reached=reached)
            if self.last_command and self.last_command.get('port')==port and self.last_command.get('id')==i:
                self.last_command.update(outcome='feedback',position=pos,reached=reached,moved=delta>=2)
            if reached:self.feedback_window.pop(key,None)
            else:self.feedback_window[key]={'origin':pos,'at':time.monotonic()}

    def snapshot(self):
        with self.io:
            result=super().snapshot()
            setup=self.whole_robot_setup()
            preferred='preview' if isinstance(self,PreviewRuntime) else 'hardware' if setup['ready'] else 'preset'
            result.update(version='3.8-manual-pose',control_profile=self.control_profile,
                          preferred_target=preferred,
                          original=self.engine.snapshot(),original_readiness=self.readiness(),
                          trial_readiness=self.readiness('original_joint'),
                          preset_readiness=self.preset_readiness() if not isinstance(self,PreviewRuntime) else {},
                          preset_blocked=self.preset_blocked,joint_faults={k:v['reason'] for k,v in self.joint_faults.items()},
                          stall_marks=self.stall_marks,saved_ranges=self.saved_ranges(),
                          limit_selftest=self.limit_test.snapshot(),
                          whole_robot_setup=setup,hardware_preparation=self.preparation,service_blocked=self.service_blocked,
                      manual_calibration=self.manual_calibration.snapshot(),control_settings=self.settings.snapshot())
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
