"""Assistant-driven runs: step sequences that reuse the self-test's safe motion.

The assistant (over SSH, on the laptop itself) submits a list of steps; they
run in the background under the same exclusion, abort and cleanup rules as
the limit self-test (see AGENT_CONTROL_ZH.md). Every run writes its plan, a
step log and any images into its own evidence folder.
"""
from __future__ import annotations
from datetime import datetime
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from .control_runtime import now
from .limit_selftest import ARM_TESTS,HEAD_TESTS,BACKOFF_RAW,MOVE_TORQUE,RETURN_SPEED_DEG,Aborted,Blocked
from .torque_control import EXPECTED,read_registers,word,write_register
from .whole_calibration import atomic_json

MOTION_OPS={'move','probe','backoff_trial','rotate','advance','travel','drive'}
BASE_OPS=('rotate','advance','travel','drive')
OPS=MOTION_OPS|{'snapshot','sample','wait'}
ARM_KINDS={1:'pan',2:'lift',3:'elbow',4:'pitch',5:'roll',6:'grip'}
HEAD_KINDS={7:'pan',8:'tilt'}
LAPTOP_CAMERA='HD Camera'
# Folded-arm shoulder pan centres and the raw direction that turns each arm
# toward the robot's front centre (measured 2026-11-02). A shoulder-pan probe
# sweeps the folded arm inward, so the other arm must not be parked there: on
# 2026-11-02 the right gripper hit the left observer's gripper at +45 deg.
PAN_CENTRE={'left':2037,'right':2045};PAN_INWARD={'left':1,'right':-1};PAN_CLEAR_RAW=228
# An explicit move torque is a trial or an escalation step (AGENT_CONTROL_ZH.md
# section 10): at most 75 %, 5 points under the servo's 80 % overload
# threshold - beyond it the overload protection cuts the output after ~2 s,
# so more ceiling adds heat, not force. Omitting torque keeps full torque.
EXPLICIT_TORQUE_MAX=750
# A move that stalls with its load peak under 25 % is a pose-related contact
# far more often than a hard stop (right shoulder lift, 2026-11-02).
LOW_LOAD_PEAK=250


class AgentRunner:
    def __init__(self,runtime,root,ffmpeg='ffmpeg'):
        self.runtime=runtime;self.root=Path(root);self.ffmpeg=ffmpeg
        self.active=False;self.thread=None;self.run_id=None;self.dir=None;self.label=''
        self.steps=[];self.results=[];self.current=None;self.error=None
        self.started_at=None;self.finished_at=None
        self.consumed_drive_evidence=set()
        self.consumed_return_evidence=set()
        # Set by the controller app: latest JPEG from an active shared stream.
        self.shared_frame=None

    @property
    def lt(self):return self.runtime.limit_test

    # ---- addressing ------------------------------------------------------
    def resolve(self,key):
        """'COM4:2', 'left:2', 'right:3', 'head:7' -> (port, id)."""
        side,_,raw=str(key).partition(':')
        try:i=int(raw)
        except ValueError:raise ValueError(f'关节写法无效：{key}') from None
        if side in ('left','right'):port=self.runtime.arm_ports()[side]
        elif side=='head':port='COM3'
        else:port=side
        if port not in EXPECTED or i not in EXPECTED[port]:raise ValueError(f'未知关节：{key}')
        if port=='COM4' and i in (9,10):raise ValueError('助手运行不驱动底盘轮')
        if side=='head' and i not in (7,8):raise ValueError(f'头部只有7、8：{key}')
        return port,i

    def _cfg(self,port,i):
        if port=='COM3' and i in HEAD_KINDS:return HEAD_TESTS[HEAD_KINDS[i]]
        return ARM_TESTS[ARM_KINDS[i]]

    # ---- control ---------------------------------------------------------
    def snapshot(self):
        lt=self.lt
        return {'running':self.active,'run_id':self.run_id,'dir':str(self.dir) if self.dir else None,'label':self.label,
                'current':self.current,'steps':len(self.steps),'results':list(self.results),'error':self.error,
                'started_at':self.started_at,'finished_at':self.finished_at,
                'live':dict(lt.live) if self.active else {},'events':list(lt.events)[-10:] if self.run_id else []}

    def _validate(self,steps):
        if not isinstance(steps,list) or not steps:raise ValueError('至少需要一个步骤')
        advances=[n for n,s in enumerate(steps) if isinstance(s,dict) and s.get('op') in ('advance','travel','drive')]
        if advances and (advances!=[0] or any(s.get('op')!='snapshot' for s in steps[1:])):
            raise ValueError('每次只允许一个直行脉冲，随后只拍照；重新观察后才能再发')
        for n,step in enumerate(steps):
            op=step.get('op') if isinstance(step,dict) else None
            if op not in OPS:raise ValueError(f'第{n+1}步操作未知：{op}')
            if op=='move':
                if not step.get('targets'):raise ValueError(f'第{n+1}步缺少目标')
                for key in step['targets']:self.resolve(key)
            if op=='rotate':
                # Survey rotation is a separate, bounded primitive. Joint move
                # still refuses wheel targets; translation is never accepted.
                if set(step)-{'op','direction','duration','max_raw','label','continue_on_fail'}:
                    raise ValueError('原地扫描不接受平移或其他参数')
                direction=step.get('direction',1);duration=step.get('duration',.18);raw=step.get('max_raw',160)
                import math
                if type(direction) is not int or direction not in (-1,1) or type(raw) is not int or not 60<=raw<=160:
                    raise ValueError('原地扫描转向/轮速无效')
                if isinstance(duration,bool) or not isinstance(duration,(int,float)) or not math.isfinite(duration) or not .04<=duration<=.65:
                    raise ValueError('原地扫描脉冲只能40–650ms')
            if op=='advance':
                if set(step)-{'op','duration','max_raw','label','observation_run_id','clearance_confirmed'}:
                    raise ValueError('直行观察不接受转向、倒车、续跑或其他参数')
                duration=step.get('duration',.18);raw=step.get('max_raw',80)
                import math
                if (isinstance(duration,bool) or not isinstance(duration,(int,float)) or not math.isfinite(duration)
                        or not .04<=duration<=.65 or type(raw) is not int or not 60<=raw<=120):
                    raise ValueError('直行观察仅允许40–650ms、60–120原始轮速')
                if step.get('clearance_confirmed') is not True:
                    raise ValueError('需先根据新照片确认短行程地面、人员和车体边界')
                if step.get('observation_run_id')!=self.run_id:
                    raise ValueError('直行需引用最近一次观察运行')
                self._advance_evidence(step)
            if op=='travel':
                if set(step)-{'op','distance_m','max_raw','label','observation_run_id','clearance_confirmed','boundary_id','return_run_id'}:
                    raise ValueError('小幅行走不接受转向或续跑参数')
                distance=step.get('distance_m');raw=step.get('max_raw',220)
                import math
                if (isinstance(distance,bool) or not isinstance(distance,(int,float)) or not math.isfinite(distance)
                        or not .005<=abs(distance)<=.05 or type(raw) is not int or not 60<=raw<=220):
                    raise ValueError('小幅行走限正负0.5–5cm估计距离、60–220原始轮速')
                if not re.fullmatch(r'[a-z0-9-]{1,64}',str(step.get('boundary_id',''))):
                    raise ValueError('需要绑定本次用户指定的边界依据')
                if step.get('clearance_confirmed') is not True or step.get('observation_run_id')!=self.run_id:
                    raise ValueError('先检查最近一次停车照片和本次边界')
                self._advance_evidence(step)
                if distance<0:self._return_evidence(step)
                elif 'return_run_id' in step:raise ValueError('只有原路后退才接受返回依据')
            if op=='drive':
                if set(step)-{'op','distance_m','max_raw','stop_margin_m','label','observation_run_id','clearance_confirmed','boundary_id'}:
                    raise ValueError('避障行驶不接受转向、倒车或续跑参数')
                distance=step.get('distance_m');raw=step.get('max_raw',160);margin=step.get('stop_margin_m',.12)
                import math
                if (isinstance(distance,bool) or not isinstance(distance,(int,float)) or not math.isfinite(distance)
                        or not .05<=distance<=.6 or type(raw) is not int or not 60<=raw<=220):
                    raise ValueError('避障行驶限0.05–0.6m前进、60–220原始轮速')
                if isinstance(margin,bool) or not isinstance(margin,(int,float)) or not .08<=margin<=.5:
                    raise ValueError('停车余量需在0.08–0.5m')
                if not re.fullmatch(r'[a-z0-9-]{1,64}',str(step.get('boundary_id',''))):
                    raise ValueError('需要绑定本次用户指定的边界依据')
                if step.get('clearance_confirmed') is not True or step.get('observation_run_id')!=self.run_id:
                    raise ValueError('先检查最近一次停车照片和本次边界')
                if getattr(self.runtime,'perception_gate',None) is None:
                    raise ValueError('控制服务未接入视觉避障闸门')
                self._advance_evidence(step)
            if op=='probe':self.lt._ordered([step.get('item')])
            if op in ('sample','backoff_trial'):
                for key in (step.get('joints') or [step.get('joint')]):self.resolve(key)
            if op=='backoff_trial' and step.get('variant','A') not in ('A','B','C','D','S'):raise ValueError('退离实验只有A/B/C/D/S')
            if step.get('watch'):
                if op not in MOTION_OPS:raise ValueError(f'第{n+1}步：只有动作步骤可以边动边拍')
                for camera in step['watch'].get('cameras',[0]):
                    if camera!='laptop' and camera not in (0,1,2):raise ValueError(f'第{n+1}步相机编号无效：{camera}')

    def start(self,steps,confirmed,label='',background=True):
        r=self.runtime;lt=self.lt
        self._validate(steps)
        with r.io:
            if lt.running:raise ValueError('自检或助手运行正在进行')
            if not confirmed:raise ValueError('请确认周围空间足够、无人靠近')
            if r.controller['controller']=='online':raise ValueError('手柄接管中，先结束接管')
            if r.manual_calibration.active:raise ValueError('采集中，先结束采集')
            if lt.require_pad and lt._pad_active():raise ValueError('手柄全部回中后再开始：运行中按手柄任意键会立即停止')
            if any(s['op'] in MOTION_OPS for s in steps):r.scan();r.prepare_hardware(True)
            lt.running=True;lt.abort_reason=None;lt.error=None;lt.current='agent';lt.live={};lt.plan=[];lt.results={}
            lt.torque_backup={};lt.limit_backup={};lt.finalized=set();lt.arm_origin={};lt.events.clear()
            lt.started_at=now();lt.finished_at=None
            self.active=True;self.steps=[dict(s) for s in steps];self.results=[];self.current=None;self.error=None
            self.label=str(label)[:80];self.run_id=time.strftime('%Y%m%d-%H%M%S');self.dir=self.root/self.run_id
            self.dir.mkdir(parents=True,exist_ok=True);self.started_at=now();self.finished_at=None
            atomic_json(self.dir/'plan.json',{'label':self.label,'steps':self.steps,'started_at':self.started_at})
            lt._log(f'助手运行开始：{self.label or self.run_id}，共{len(steps)}步')
        if background:
            self.thread=threading.Thread(target=self._run,daemon=True);self.thread.start()
        else:self._run()
        return self.snapshot()

    def _record(self,n,step,result):
        entry={'step':n,'op':step['op'],'at':now(),**result}
        self.results.append(entry)
        with open(self.dir/'steps.jsonl','a',encoding='utf-8') as f:f.write(json.dumps(entry,ensure_ascii=False)+'\n')

    def _run(self):
        lt=self.lt
        try:
            for n,step in enumerate(self.steps):
                lt._check_abort();self.current=n;lt.current=f'agent:{n+1}/{len(self.steps)}:{step["op"]}'
                watcher=self._start_watch(n,step);shots=[]
                try:
                    try:result={'status':'done',**getattr(self,'_op_'+step['op'])(n,step)}
                    finally:shots=self._stop_watch(watcher)
                except Aborted:raise
                except Exception as exc:
                    result={'status':'failed','message':str(exc)}
                    if getattr(exc,'contact',None):result['contact']=exc.contact
                    if getattr(exc,'rotation',None):result['rotation']=exc.rotation
                    if shots:result['watch']=shots
                    lt._log(f'助手第{n+1}步（{step["op"]}）失败：{exc}')
                    self._record(n,step,result)
                    if isinstance(exc,OSError):raise
                    if step['op'] in BASE_OPS or not step.get('continue_on_fail',step['op'] not in ('move','backoff_trial')):
                        self.error=f'第{n+1}步失败，运行停止：{exc}';break
                    continue
                if shots:result['watch']=shots
                self._record(n,step,result)
        except Aborted as exc:
            self.error=str(exc);lt._log(str(exc))
        except Exception as exc:
            self.error='助手运行中断：'+str(exc);lt._log(self.error)
        finally:
            if any(s['op'] in BASE_OPS for s in self.steps):
                failures=[]
                with self.runtime.io:
                    for address,length in ((46,2),(40,1)):
                        for motor in (9,10):
                            try:write_register(self.runtime.session.ports['COM4'],motor,address,0,length)
                            except Exception as exc:failures.append(str(exc))
                    self.runtime.wheels_commanded=False;self.runtime.wheel_goals=[0,0];self.runtime.engine.wheels=[0,0]
                if failures:self.error=(self.error or '')+'；轮组清理失败：'+'; '.join(failures)
            lt._cleanup()
            lt.running=False;lt.current=None;lt.finished_at=now()
            self.active=False;self.current=None;self.finished_at=now()
            try:atomic_json(self.dir/'result.json',{'label':self.label,'started_at':self.started_at,'finished_at':self.finished_at,
                                                    'error':self.error,'results':self.results,'events':list(lt.events)})
            except OSError:pass
            lt._log('助手运行结束'+('：'+self.error if self.error else ''))

    # ---- steps -----------------------------------------------------------
    def _op_rotate(self,n,step):
        from .rotation_scan import bounded_rotate
        return bounded_rotate(self.runtime,self.lt._check_abort,step.get('direction',1),
                              step.get('duration',.18),step.get('max_raw',160))

    def _advance_evidence(self,step):
        """Evidence limits stale/repeated commands; it is not automatic vision."""
        rid=step.get('observation_run_id','')
        if not isinstance(rid,str) or not re.fullmatch(r'\d{8}-\d{6}',rid) or rid in self.consumed_drive_evidence:
            raise ValueError('直行观察依据无效或已用过，请重新拍照')
        prior=json.loads((self.root/rid/'result.json').read_text(encoding='utf-8'))
        if prior.get('error'):raise ValueError('不能使用失败运行的照片驱动')
        rows=[s for s in prior['results'] if s['op']=='snapshot' and s['status']=='done']
        if not rows:raise ValueError('缺少停车观察照片')
        row=rows[-1]
        captured=datetime.fromisoformat(row.get('capture_started_at',row['at']))
        age=(datetime.now().astimezone()-captured).total_seconds()
        cameras={s['camera'] for s in row['shots'] if 'file' in s and (self.root/rid/s['file']).is_file()}
        if not 0<=age<=60 or 0 not in cameras or not cameras.intersection({1,2}):
            raise ValueError('需60秒内头部和至少一路低处相机照片，再人工判断通路')
        positions=row.get('wheel_positions',{})
        if set(positions)!={'COM4:9','COM4:10'}:raise ValueError('照片缺少轮组位置，需重新拍照')
        current=self.runtime.motors()
        for key,pos in {**row['positions'],**positions}.items():
            port,raw=key.split(':');i=int(raw)
            delta=(current[(port,i)]['position']-pos+2048)%4096-2048
            if abs(delta)>(2 if i in (9,10) else 10):
                raise ValueError('拍照后姿态或轮组位置已变，请重新观察')
        return {'run_id':rid,'age_s':round(age,2),'visual_clearance':'supervisor_assessed_not_automatic'}

    def _op_advance(self,n,step):
        from .rotation_scan import bounded_advance
        evidence=self._advance_evidence(step)
        self.consumed_drive_evidence.add(evidence['run_id'])
        return {**bounded_advance(self.runtime,self.lt._check_abort,step.get('duration',.18),step.get('max_raw',80)),
                'observation':evidence}

    def _op_travel(self,n,step):
        from .rotation_scan import bounded_travel
        evidence=self._advance_evidence(step)
        returning=self._return_evidence(step) if step['distance_m']<0 else None
        self.consumed_drive_evidence.add(evidence['run_id'])
        if returning:self.consumed_return_evidence.add(returning['run_id'])
        return {**bounded_travel(self.runtime,self.lt._check_abort,step['distance_m'],step.get('max_raw',220)),
                'observation':evidence,'boundary_id':step['boundary_id'],
                **({'return_path':returning} if returning else {}),
                'boundary_enforcement':'supervisor_visual_reference_not_metric_geofence'}

    def _return_evidence(self,step):
        """Reverse only the immediately preceding, successfully cleared leg.

        This constrains wheel rotation, not people moving into a blind area.
        Fresh visual clearance is independently required for each return.
        """
        rid=step.get('return_run_id','')
        if (not isinstance(rid,str) or not re.fullmatch(r'\d{8}-\d{6}',rid)
                or rid in self.consumed_return_evidence):
            raise ValueError('后退需引用尚未返回过的前进路段')
        prior=json.loads((self.root/rid/'result.json').read_text(encoding='utf-8'))
        moves=[r for r in prior.get('results',[]) if r.get('op')=='travel' and r.get('status')=='done']
        if prior.get('error') or len(moves)!=1:raise ValueError('返回依据必须是成功的单段前进')
        row=moves[0]
        age=(datetime.now().astimezone()-datetime.fromisoformat(row['at'])).total_seconds()
        if (row.get('direction')!='forward' or row.get('boundary_id')!=step['boundary_id']
                or not 0<=age<=300 or abs(step['distance_m'])>row.get('requested_distance_m',0)):
            raise ValueError('后退超出本次边界、前进距离或五分钟返回窗口')
        current=self.runtime.motors()
        for motor in (9,10):
            stop=row['stop_readback'][str(motor)]
            delta=(current[('COM4',motor)]['position']-stop['position']+2048)%4096-2048
            if abs(delta)>2:raise ValueError('已离开前进终点，不能使用旧路段后退')
        return {'run_id':rid,'age_s':round(age,2),'kind':'previous_leg_only_not_rear_obstacle_detection'}

    def _op_drive(self,n,step):
        from .rotation_scan import bounded_drive
        evidence=self._advance_evidence(step)
        self.consumed_drive_evidence.add(evidence['run_id'])
        gate=self.runtime.perception_gate
        return {**bounded_drive(self.runtime,self.lt._check_abort,gate,step['distance_m'],step.get('max_raw',160),
                                step.get('stop_margin_m',.12)),
                'observation':evidence,'boundary_id':step['boundary_id'],'gate_final':gate.snapshot(),
                'boundary_enforcement':'visual_gate_plus_supervisor_boundary_not_geofence'}

    def _watch_joints(self,step):
        try:
            if step['op']=='move':return [self.resolve(k) for k in step['targets']]
            if step['op']=='backoff_trial':return [self.resolve(step['joint'])]
            port,_=self.lt._target(step['item']);return [(port,i) for i in self.lt._joints_for(step['item'])]
        except Exception:return []

    def _start_watch(self,n,step):
        """Capture the chosen cameras in the background while a motion step
        runs, each frame stamped with time and the moving joints' positions."""
        cfg=step.get('watch')
        if not cfg:return None
        stop=threading.Event();holder={'shots':[]};joints=self._watch_joints(step)
        cameras=list(cfg.get('cameras',[0]));every=max(1.,float(cfg.get('every',2)));limit=max(1,min(60,int(cfg.get('max',20))))
        def loop():
            t0=time.monotonic();k=0
            while not stop.is_set() and k<limit:
                begin=time.monotonic()
                for camera in cameras:
                    path=self.dir/f'{n:02d}-watch-{k:02d}-cam{camera}.jpg'
                    try:
                        if camera!='laptop' and self._camera_busy(camera):raise RuntimeError('该相机正在网页上推流')
                        self._capture(camera,path)
                        try:positions=self._positions(joints)
                        except Exception:positions={}
                        holder['shots'].append({'camera':camera,'file':path.name,'t':round(time.monotonic()-t0,1),'positions':positions})
                    except Exception as exc:holder['shots'].append({'camera':camera,'error':str(exc)[:120]})
                k+=1
                stop.wait(max(0.,every-(time.monotonic()-begin)))
        thread=threading.Thread(target=loop,daemon=True);thread.start()
        return stop,thread,holder

    def _stop_watch(self,watcher):
        if not watcher:return []
        stop,thread,holder=watcher;stop.set();thread.join(timeout=20)
        return holder['shots']

    def _positions(self,joints):
        out={}
        for port,i in joints:out[f'{port}:{i}']=word(self.lt._read(port,i),56)
        return out

    def _op_move(self,n,step):
        targets={};start={}
        for key,value in step['targets'].items():
            port,i=self.resolve(key);reg=self.lt._read(port,i);low,high=word(reg,9),word(reg,11);value=int(value)
            if not low<=value<=high:raise ValueError(f'{port}:{i}目标{value}超出当前限位{low}–{high}；越限只能用探测')
            targets[(port,i)]=value;start[(port,i)]=word(reg,56)
        speed=max(3,min(30,float(step.get('speed_deg',12))))
        torque=step.get('torque');torque=max(150,min(EXPLICIT_TORQUE_MAX,int(torque))) if torque else None
        try:
            self.lt._move_retry(targets,speed,step.get('label') or '助手移动',strict=bool(step.get('strict')),
                                torque=torque,tries=int(step.get('tries',3)))
        except Blocked as exc:
            exc.contact=self._contact(n,exc,start,speed,torque);raise
        return {'positions':self._positions(targets)}

    def _pose(self):
        """Both arms and the head, by logical name, for the contact record."""
        out={}
        joints=[(side,port,j) for side,port in self.runtime.arm_ports().items() for j in range(1,7)]+[('head','COM3',7),('head','COM3',8)]
        for side,port,j in joints:
            try:out[f'{side}:{j}']=word(self.lt._read(port,j),56)
            except Exception:out[f'{side}:{j}']=None
        return out

    def _contact(self,n,exc,start,speed,torque):
        """A blocked move is a suspected contact, not a verdict yet. The goal
        already sits at the joint's position; let the load fade (no back-off,
        no extra force) and record what the contact check needs: where and how
        hard it stopped, the whole pose and the servo state. The run ends and
        the joint stays there for observation (AGENT_CONTROL_ZH.md section 10)."""
        port,i=exc.port,exc.id;d=dict(exc.detail);name=self.lt._label(port,i)
        settle=self.lt._settle(port,i)
        reg=self.lt._read(port,i);low,high=word(reg,9),word(reg,11)
        pos=d.get('position',word(reg,56));origin=start.get((port,i),pos);target=d.get('target',pos)
        direction=1 if target>=origin else -1
        to_limit=((high if direction>0 else low)-pos)*direction
        peak=d.get('peak_load',0);low_load=peak<LOW_LOAD_PEAK
        contact={'step':n,'joint':f'{port}:{i}','name':name,'from':origin,'direction':direction,**d,'settle_ms':settle,
                 'to_limit':to_limit,'to_limit_deg':round(to_limit*360/4096,1),'requested_torque':torque,'speed_deg':speed,
                 'status':reg[65],'volt':reg[62]/10,'temp':reg[63],'load_after':word(reg,60)&0x3ff,
                 'kind':'low_load' if low_load else 'pushing','pose':self._pose(),'verdict':'pending',
                 'hint':('峰值负载低于25%就停住，多半是姿态相关接触' if low_load else '负载已到扭矩上限的60%以上，可能碰到物体、车体、另一臂或机械止挡')
                        +'；关节保持原地，先按规范第10节观察确认，确认前不加力'}
        with open(self.dir/'contacts.jsonl','a',encoding='utf-8') as f:f.write(json.dumps(contact,ensure_ascii=False)+'\n')
        self.lt._log(f'疑似碰阻：{name}在{pos}停住（目标{target}，峰值负载{peak/10:.0f}%，回落{settle}ms，距限位{contact["to_limit_deg"]}°），保持原地待观察',
                     port=port,id=i,position=pos,peak_load=peak)
        return contact

    def _op_probe(self,n,step):
        item=step['item'];side,kind=item.split(':',1)
        if kind=='pan' and side in PAN_CENTRE:
            other='right' if side=='left' else 'left';port=self.runtime.arm_ports()[other]
            inward=(word(self.lt._read(port,1),56)-PAN_CENTRE[other])*PAN_INWARD[other]
            if inward>PAN_CLEAR_RAW:
                raise ValueError(f'{"右臂" if other=="right" else "左臂"}肩水平向内转了{inward*360/4096:.0f}°，测{"左臂" if side=="left" else "右臂"}肩水平时会扫到它；先把它转回中间')
        result=self.lt._run_item(item)
        rest=[s.get('item') for s in self.steps[n+1:] if s.get('op')=='probe']
        self.lt._restore_arm(item,rest)
        return {'item':item,**result}

    def _op_wait(self,n,step):
        seconds=max(0.,min(30.,float(step.get('seconds',1))));self.lt._wait(seconds)
        return {'seconds':seconds}

    def _camera_busy(self,camera):
        health=self.runtime.camera_health[camera]
        return health.get('state') in ('在线','连接中')

    def _capture(self,camera,path):
        if camera=='laptop':args=['-video_size','1280x720','-i',f'video={LAPTOP_CAMERA}','-t','2.5']
        else:args=['-video_device_number',str(camera),'-video_size','640x480','-i','video=USB2.0_CAM1','-t','1.5']
        cmd=[self.ffmpeg,'-nostdin','-hide_banner','-loglevel','error','-f','dshow',*args,'-update','1','-q:v','3','-y',str(path)]
        done=subprocess.run(cmd,capture_output=True,timeout=15)
        if done.returncode!=0 or not path.is_file():
            raise RuntimeError((done.stderr or b'').decode('utf-8','replace').strip()[-200:] or 'ffmpeg没有输出图像')

    def _op_snapshot(self,n,step):
        capture_started_at=now()
        tag=''.join(c for c in str(step.get('tag','shot')) if c.isalnum() or c in '-_')[:40] or 'shot'
        shots=[]
        for camera in step.get('cameras',[0,1,2,'laptop']):
            if camera!='laptop' and camera not in (0,1,2):raise ValueError(f'相机编号无效：{camera}')
            path=self.dir/f'{n:02d}-{tag}-cam{camera}.jpg'
            try:
                if camera!='laptop' and self._camera_busy(camera):
                    # The live stream owns the device; reuse its newest frame instead of reopening it.
                    if not self.shared_frame:raise RuntimeError('该相机正在网页上推流，暂不抓单帧')
                    path.write_bytes(self.shared_frame(camera))
                    shots.append({'camera':camera,'file':path.name,'bytes':path.stat().st_size,'source':'shared_stream'})
                    continue
                self._capture(camera,path);shots.append({'camera':camera,'file':path.name,'bytes':path.stat().st_size})
            except Exception as exc:shots.append({'camera':camera,'error':str(exc)})
        if not any('file' in s for s in shots):raise RuntimeError('没有抓到任何图像：'+'；'.join(f'{s["camera"]}:{s.get("error")}' for s in shots))
        return {'shots':shots,'capture_started_at':capture_started_at,
                'positions':self._positions([(p,i) for p,ids in EXPECTED.items() for i in ids if not (p=='COM4' and i in (9,10))]),
                'wheel_positions':self._positions([('COM4',9),('COM4',10)])}

    def _read_sample(self,port,i,start):
        reg=self.lt._read(port,i);load=word(reg,60)
        return {'t':round(time.monotonic()-start,4),'joint':f'{port}:{i}','pos':word(reg,56),'goal':word(reg,42),
                'load':(load&0x3ff)*(-1 if load&0x400 else 1),'torque_limit':word(reg,48),'status':reg[65],
                'moving':reg[66],'current_lo':reg[69],'volt':reg[62],'temp':reg[63],'torque':reg[40]}

    def _op_sample(self,n,step):
        joints=[self.resolve(k) for k in step['joints']]
        seconds=max(.05,min(10.,float(step.get('seconds',1))));period=1/max(1.,min(50.,float(step.get('hz',20))))
        path=self.dir/f'{n:02d}-sample.jsonl';start=time.monotonic();count=0
        with open(path,'w',encoding='utf-8') as f:
            while time.monotonic()-start<seconds:
                self.lt._check_abort()
                for port,i in joints:f.write(json.dumps(self._read_sample(port,i,start))+'\n');count+=1
                time.sleep(period)
        return {'file':path.name,'samples':count}

    def _op_backoff_trial(self,n,step):
        """Press one joint into a stop, then back off in a chosen way while
        logging how long the encoder takes to move. A: at once. B: after
        delay s. C: ease the goal 5 counts off the stop first. D: drop the
        torque ceiling to 0 for 0.2 s first (wrist and gripper only). S: the
        self-test's fix - the probe already waited for the load to fade.
        Variants other than S skip that wait by measuring it inside the probe
        and reporting it, so the latency is counted from the stall."""
        lt=self.lt;port,i=self.resolve(step['joint']);cfg=self._cfg(port,i)
        direction=-1 if int(step.get('direction',-1))<0 else 1;variant=step.get('variant','A')
        if variant not in ('A','B','C','D','S'):raise ValueError('退离实验只有A/B/C/D/S')
        if variant=='D' and ARM_KINDS.get(i) not in ('pitch','roll','grip') and not (port=='COM3' and i in HEAD_KINDS):
            raise ValueError('D实验只在腕、夹爪或头部做，避免掉臂')
        start=word(lt._read(port,i),56)
        settle=lt._settle
        if variant!='S':lt._settle=lambda port,i:0
        try:stop=lt._probe(port,i,direction,cfg,backoff=False)
        finally:lt._settle=settle
        if stop['kind']!='stall':
            lt._move_retry({(port,i):start},max(cfg['speed_deg'],RETURN_SPEED_DEG),'回到起点')
            return {'variant':variant,'edge':stop['edge'],'kind':stop['kind'],'latency_ms':None,'message':'没有顶到止挡'}
        edge=stop['edge'];bus=self.runtime.session.ports[port];full=min(MOVE_TORQUE,lt.torque_backup.get(f'{port}:{i}',1000))
        if variant=='B':lt._wait(max(0.,min(5.,float(step.get('delay',1)))))
        if variant=='C':
            with self.runtime.io:write_register(bus,i,42,edge-direction*5,2)
            lt._wait(.3)
        if variant=='D':
            with self.runtime.io:write_register(bus,i,48,0,2)
            lt._wait(.2)
        target=edge-direction*BACKOFF_RAW;speed=cfg['speed_deg']*4096/360
        path=self.dir/f'{n:02d}-backoff-{variant}.jsonl';t0=time.monotonic();latency=None;rows=[]
        with self.runtime.io:
            write_register(bus,i,48,full,2);write_register(bus,i,46,min(1000,max(60,int(speed))),2);write_register(bus,i,42,target,2)
        while time.monotonic()-t0<3:
            lt._check_abort();row=self._read_sample(port,i,t0);rows.append(row)
            # Only motion away from the stop counts; the servo first sinks deeper.
            if latency is None and (row['pos']-edge)*-direction>=8:latency=round(row['t']*1000)
            if abs(row['pos']-target)<=8:break
            time.sleep(.01)
        with open(path,'w',encoding='utf-8') as f:f.write(''.join(json.dumps(r)+'\n' for r in rows))
        moved=(rows[-1]['pos']-edge)*-direction if rows else 0
        deeper=max([0]+[(edge-r['pos'])*-direction for r in rows])
        lt._log(f'退离实验{variant}：{lt._label(port,i)}止挡{edge}，'+(f'{latency}ms后开始往回移动' if latency is not None else '3秒内未往回移动')+f'，先往里压{deeper}计数，共退{moved}计数')
        lt._move_retry({(port,i):start},max(cfg['speed_deg'],RETURN_SPEED_DEG),'回到起点')
        return {'variant':variant,'delay':step.get('delay'),'edge':edge,'latency_ms':latency,'moved':moved,'deeper':deeper,
                'settle_ms':stop.get('settle_ms',0),'file':path.name,'peak_load':stop['peak_load']}

