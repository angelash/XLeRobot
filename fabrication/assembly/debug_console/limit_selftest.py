"""Powered joint-limit self-test: slow, torque-limited probing to a hard stop.

Each selected joint is driven slowly with a reduced torque ceiling (SRAM 48)
until the encoder stops following the goal (hard stop or interference) or a
bounded exploration span beyond the saved limit is used up. The stop point,
moved inward by a margin, becomes the new servo limit. Joints that could
interfere are pre-posed first and everything returns to its start pose.
Any gamepad input, a lost gamepad or the page Stop button aborts at once.
"""
from __future__ import annotations
from collections import deque
from copy import deepcopy
import json
import threading
import time
from pathlib import Path

from .control_runtime import EXPECTED_SERIALS,now
from .torque_control import read_registers,word,write_register
from .upstream_teleop import neutral
from .whole_calibration import atomic_json

ARM_ORDER=('grip','roll','pitch','elbow','lift','fold','pan')
ARM_TESTS={
 'grip':{'id':6,'name':'夹爪','torque':350,'speed_deg':25,'extend_deg':40,'lag':40,'prepose':{},
         'note':'张开端和闭合端分别轻顶到止挡；夹爪里不要放任何东西。'},
 'roll':{'id':5,'name':'腕旋转','torque':350,'speed_deg':20,'extend_deg':60,'lag':40,'prepose':{},
         'note':'腕旋转多半没有硬止挡、以线缆为限：在已存行程外最多再探60°，到达即停。'},
 'pitch':{'id':4,'name':'腕俯仰','torque':450,'speed_deg':15,'extend_deg':45,'lag':48,'prepose':{3:'mid'},
          'note':'先把肘摆到行程中位给夹爪留空间；夹爪碰到前臂即记为该端限位。'},
 'elbow':{'id':3,'name':'肘','torque':600,'speed_deg':12,'extend_deg':40,'lag':56,'prepose':{4:'mid'},
          'note':'先把腕俯仰摆到中位；收起端可能被夹爪或上臂挡住，受阻即为实际可用限位。'},
 'lift':{'id':2,'name':'肩抬升','torque':800,'speed_deg':10,'extend_deg':30,'lag':64,'prepose':{3:'mid',4:'mid'},
         'note':'带动整条手臂，扭矩上限较高；先把肘、腕俯仰放到中位。'},
 'fold':{'name':'收起此臂','note':'把肩抬升、肘、腕俯仰同步移回保存行程时的折叠姿态，供肩水平测试。'},
 'pan':{'id':1,'name':'肩水平','torque':500,'speed_deg':12,'extend_deg':40,'lag':48,'prepose':{},'requires_fold':True,
        'note':'必须先全收起该臂，否则会扫到车体或另一条臂。'},
}
HEAD_TESTS={
 'tilt':{'id':8,'name':'头部俯仰','torque':350,'speed_deg':15,'extend_deg':30,'lag':40,'prepose':{},'note':'镜头上下转到止挡，注意线缆余量。'},
 'pan':{'id':7,'name':'头部水平','torque':350,'speed_deg':15,'extend_deg':40,'lag':40,'prepose':{},'note':'镜头左右转到止挡。'},
}
RECOMMENDED=['head:tilt','head:pan',*[f'{s}:{k}' for s in ('left','right') for k in ARM_ORDER]]
MARGIN_RAW=24;BACKOFF_RAW=32;TICK=.05;WINDOW=8;FOLD_TOL=60;MOVE_TORQUE=700


class Aborted(Exception):pass


def deg(raw):return round(raw*360/4096,1)


class LimitSelfTest:
    def __init__(self,runtime,results_dir,profile_path,tick=TICK):
        self.runtime=runtime;self.results_dir=Path(results_dir);self.profile_path=Path(profile_path)
        self.tick=tick;self.require_pad=True
        self.running=False;self.abort_reason=None;self.thread=None;self.plan=[];self.current=None
        self.results={};self.events=deque(maxlen=60);self.apply=True;self.error=None;self.live={}
        self.torque_backup={};self.limit_backup={};self.finalized=set();self.started_at=None;self.finished_at=None

    # ---- catalogue -------------------------------------------------------
    def _target(self,item):
        side,kind=item.split(':',1)
        if side=='head':
            if kind not in HEAD_TESTS:raise ValueError('未知自检项目：'+item)
            return 'COM3',HEAD_TESTS[kind]
        if side not in ('left','right') or kind not in ARM_TESTS:raise ValueError('未知自检项目：'+item)
        return self.runtime.arm_ports()[side],ARM_TESTS[kind]

    def _profile(self):
        try:return json.loads(self.profile_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):return {}

    def fold_pose(self,side):
        port=self.runtime.arm_ports()[side];joints=self._profile().get('joints',{});pose={}
        for i in (2,3,4):
            entry=joints.get(f'{port}:{i}') or {};m=self.runtime.motors().get((port,i))
            if not m or 'position' not in entry or entry.get('homing_raw')!=m['homing_raw']:return None
            pose[i]=max(m['min'],min(m['max'],int(entry['position'])))
        return pose

    def folded(self,side):
        pose=self.fold_pose(side)
        if not pose:return None
        port=self.runtime.arm_ports()[side];motors=self.runtime.motors()
        return all(abs(motors[(port,i)]['position']-pose[i])<=FOLD_TOL for i in (2,3))

    def items(self):
        motors=self.runtime.motors();out=[]
        groups=[('head',k) for k in ('tilt','pan')]+[(s,k) for s in ('left','right') for k in ARM_ORDER]
        for side,kind in groups:
            item=f'{side}:{kind}';port,cfg=self._target(item)
            row={'id':item,'side':side,'kind':kind,'name':cfg['name'],'note':cfg['note'],'port':port,
                 'side_label':{'head':'头部','left':'左臂','right':'右臂'}[side],
                 'prepose':[ARM_TESTS[k]['name'] for k,v in ARM_TESTS.items() if v.get('id') in cfg.get('prepose',{})],
                 'result':self.results.get(item)}
            if 'id' in cfg:
                m=motors.get((port,cfg['id']))
                if m:row.update(motor=cfg['id'],position=m['position'],min=m['min'],max=m['max'],range_deg=deg(m['max']-m['min']),torque=m['torque'])
            if kind in ('pan','fold') and side!='head':row['folded']=self.folded(side)
            out.append(row)
        return out

    def snapshot(self):
        return {'running':self.running,'current':self.current,'plan':list(self.plan),'results':deepcopy(self.results),
                'error':self.error,'live':dict(self.live),'items':self.items(),'recommended':RECOMMENDED,
                'events':list(self.events),'apply':self.apply,'started_at':self.started_at,'finished_at':self.finished_at}

    # ---- control ---------------------------------------------------------
    def _ordered(self,items):
        wanted=set(items or [])
        if not wanted:raise ValueError('请至少勾选一个自检项目')
        unknown=wanted-set(RECOMMENDED)
        if unknown:raise ValueError('未知自检项目：'+'、'.join(sorted(unknown)))
        return [x for x in RECOMMENDED if x in wanted]

    def start(self,items,confirmed,apply=True,background=True):
        r=self.runtime
        with r.io:
            if self.running:raise ValueError('自检正在进行')
            if not confirmed:raise ValueError('请确认周围空间足够、无人靠近；自检中按手柄任意键立即停止')
            if r.controller['controller']=='online':raise ValueError('先结束手柄接管，再开始自检')
            if r.manual_calibration.active:raise ValueError('先结束采集，再开始自检')
            # A sleeping gamepad does not block a delegated run; a connected one
            # must be neutral so that any later input means "stop".
            if self.require_pad and self._pad_active():
                raise ValueError('手柄全部回中后再开始：自检过程中按手柄任意键会立即停止')
            plan=self._ordered(items)
            for side in ('left','right'):
                if f'{side}:pan' in plan and f'{side}:fold' not in plan and not self.folded(side):
                    raise ValueError(('左臂' if side=='left' else '右臂')+'肩水平需要全收起：勾选“收起此臂”，或先手动收起')
                if f'{side}:fold' in plan and not self.fold_pose(side):
                    raise ValueError(('左臂' if side=='left' else '右臂')+'没有保存行程时的折叠姿态，无法自动收起')
            prepared=r.prepare_hardware(True)
            ready={(x['port'],x['id']) for x in prepared['ready']}
            for item in plan:
                port,cfg=self._target(item)
                ids=[cfg['id']] if 'id' in cfg else [2,3,4]
                ids+=list(cfg.get('prepose',{}))
                missing=[f'{port}:{i}' for i in ids if (port,i) not in ready]
                if missing:raise ValueError(item+'所需关节未能保持当前位置：'+'、'.join(missing))
            self.plan=plan;self.results={x:{'status':'queued'} for x in plan};self.apply=bool(apply)
            self.running=True;self.abort_reason=None;self.error=None;self.current=None;self.live={}
            self.torque_backup={};self.limit_backup={};self.finalized=set()
            self.started_at=now();self.finished_at=None;self.events.clear()
            self._log('自检开始：'+'、'.join(plan))
        if background:
            self.thread=threading.Thread(target=self._run,daemon=True);self.thread.start()
        else:self._run()
        return self.snapshot()

    def stop(self,reason='页面停止'):
        if self.running:self.abort_reason=reason
        return self.snapshot()

    def _log(self,message,**fields):
        self.events.append({'time':now(),'message':message})
        self.runtime.log('limit_selftest',message,**fields)

    def _check_abort(self):
        if self.abort_reason:raise Aborted(self.abort_reason)
        if self.require_pad and self._pad_active():raise Aborted('检测到手柄输入，自检已立即停止')

    def _pad_active(self):
        g=self.runtime.gamepad
        return bool(g.get('connected')) and time.monotonic()-self.runtime.pad_time<=1 and not neutral(g)

    def _sleep(self):
        if self.tick:time.sleep(self.tick)

    # ---- register helpers (each under the runtime's serial lock) ---------
    def _read(self,port,i):
        with self.runtime.io:return read_registers(self.runtime.session.ports[port],i)

    def _torque_limit(self,port,i,value):
        with self.runtime.io:
            p=self.runtime.session.ports[port];key=f'{port}:{i}'
            if key not in self.torque_backup:self.torque_backup[key]=word(read_registers(p,i),48)
            write_register(p,i,48,max(1,min(1000,int(value))),2)

    def _set_limits(self,port,i,low,high):
        with self.runtime.io:
            p=self.runtime.session.ports[port];key=f'{port}:{i}';before=read_registers(p,i)
            if (word(before,9),word(before,11))==(low,high):return
            if not 0<=low<high<=4095:raise ValueError(f'{key}限位无效：{low}–{high}')
            if not low<=word(before,56)<=high:raise ValueError(f'{key}当前位置不在新限位内，未写入')
            self.limit_backup.setdefault(key,(word(before,9),word(before,11)))
            write_register(p,i,55,0,1)
            order=[(11,high),(9,low)] if low>word(before,11) else [(9,low),(11,high)]
            for address,value in order:write_register(p,i,address,value,2)
            write_register(p,i,55,before[55],1)
            after=read_registers(p,i)
            if (word(after,9),word(after,11))!=(low,high):
                raise RuntimeError(f'{key}限位写入回读不符：{word(after,9)}–{word(after,11)}')

    def _label(self,port,i):
        return self.runtime.motors().get((port,i),{}).get('label',f'{port}:{i}')

    # ---- motion primitives -----------------------------------------------
    def _move_to(self,targets,speed_deg=12,what='回位'):
        """Step several joints together toward targets; stop on obstruction."""
        if not targets:return
        speed=speed_deg*4096/360;step=max(1.,speed*max(self.tick,.05))
        state={}
        for (port,i),target in targets.items():
            reg=self._read(port,i);low,high=word(reg,9),word(reg,11)
            target=max(low,min(high,int(round(target))))
            self._torque_limit(port,i,min(MOVE_TORQUE,self.torque_backup.get(f'{port}:{i}',1000)))
            with self.runtime.io:write_register(self.runtime.session.ports[port],i,46,min(1000,max(60,int(speed))),2)
            state[(port,i)]={'goal':float(word(reg,56)),'target':target,'history':deque(maxlen=WINDOW)}
        deadline=time.monotonic()+max(abs(s['target']-s['goal']) for s in state.values())/speed+6
        while True:
            self._check_abort();done=True
            for (port,i),s in state.items():
                delta=s['target']-s['goal'];s['goal']+=max(-step,min(step,delta))
                with self.runtime.io:
                    p=self.runtime.session.ports[port];write_register(p,i,42,int(round(s['goal'])),2)
                    reg=read_registers(p,i)
                pos=word(reg,56);s['history'].append(pos)
                if reg[40]==0:raise RuntimeError(self._label(port,i)+'扭矩被关闭（可能触发过载保护）')
                lag=abs(s['goal']-pos)
                settled=len(s['history'])==WINDOW and max(s['history'])-min(s['history'])<=3
                # Gravity and the reduced torque ceiling leave a joint resting a
                # few counts short; a still joint inside the obstruction lag is there.
                if s['goal']!=s['target'] or not (abs(s['target']-pos)<=8 or (settled and lag<72)):done=False
                if lag>=72 and len(s['history'])==WINDOW and max(s['history'])-min(s['history'])<=3:
                    with self.runtime.io:write_register(self.runtime.session.ports[port],i,42,pos,2)
                    raise RuntimeError(f'{what}时{self._label(port,i)}在{pos}受阻')
            if done:return
            if time.monotonic()>deadline:raise RuntimeError(what+'超时')
            self._sleep()

    def _probe(self,port,i,direction,cfg):
        reg=self._read(port,i);pos=word(reg,56);low,high=word(reg,9),word(reg,11)
        extend=round(cfg['extend_deg']*4096/360)
        bound=max(8,low-extend) if direction<0 else min(4087,high+extend)
        self._set_limits(port,i,min(low,bound),max(high,bound))
        self._torque_limit(port,i,cfg['torque'])
        speed=cfg['speed_deg']*4096/360;step=max(1.,speed*max(self.tick,.05))
        with self.runtime.io:write_register(self.runtime.session.ports[port],i,46,min(1000,max(60,int(speed))),2)
        goal=float(pos);history=deque(maxlen=WINDOW);peak=0;kind=None
        deadline=time.monotonic()+abs(bound-pos)/speed+6
        name=self._label(port,i)
        while True:
            self._check_abort()
            goal=max(goal-step,bound) if direction<0 else min(goal+step,bound)
            with self.runtime.io:
                p=self.runtime.session.ports[port];write_register(p,i,42,int(round(goal)),2);reg=read_registers(p,i)
            pos=word(reg,56);load=word(reg,60)&0x3ff;peak=max(peak,load);history.append(pos)
            self.live={'item':self.current,'joint':name,'direction':direction,'position':pos,'goal':int(round(goal)),'load':load,'bound':bound}
            if reg[40]==0:raise RuntimeError(name+'扭矩被关闭（可能触发过载保护）')
            lag=(goal-pos)*direction
            if lag>=cfg['lag'] and len(history)==WINDOW and max(history)-min(history)<=3:kind='stall';break
            # At the exploration cap the servo settles a few counts short
            # (deadband/gravity); wait until it is still, then classify.
            if int(round(goal))==bound and len(history)==WINDOW and max(history)-min(history)<=3:
                kind='bound' if abs(bound-pos)<=max(12,cfg['lag']//2) else 'stall';break
            if time.monotonic()>deadline:kind='timeout';break
            self._sleep()
        with self.runtime.io:write_register(self.runtime.session.ports[port],i,42,pos,2)
        edge=pos
        self._log(f'{name}{"负" if direction<0 else "正"}向：'+{'stall':f'在{pos}顶到止挡（负载峰值{peak/10:.0f}%）','bound':f'到探测上限{pos}仍未遇到止挡','timeout':f'超时停在{pos}'}[kind],
                  port=port,id=i,direction=direction,edge=edge,stop_kind=kind,peak_load=peak)
        if kind=='stall':self._move_to({(port,i):edge-direction*BACKOFF_RAW},cfg['speed_deg'],'退离止挡')
        return {'edge':edge,'kind':kind,'peak_load':peak}

    # ---- items -------------------------------------------------------------
    def _fold(self,side):
        port=self.runtime.arm_ports()[side];pose=self.fold_pose(side)
        if not pose:raise ValueError('没有折叠姿态记录')
        self._move_to({(port,i):pose[i] for i in (2,3,4)},10,'收起')
        return {'status':'done','message':'已收起到保存行程时的折叠姿态'}

    def _run_item(self,item):
        side,kind=item.split(':',1)
        if kind=='fold':return self._fold(side)
        port,cfg=self._target(item);i=cfg['id'];key=f'{port}:{i}'
        reg=self._read(port,i);start=word(reg,56);old=(word(reg,9),word(reg,11))
        moved=[]
        try:
            for j in cfg.get('prepose',{}):
                r2=self._read(port,j);moved.append((j,word(r2,56)))
                self._move_to({(port,j):(word(r2,9)+word(r2,11))/2},12,'预置姿态')
            low=self._probe(port,i,-1,cfg)
            self._move_to({(port,i):start},cfg['speed_deg'],'回到起点')
            high=self._probe(port,i,1,cfg)
            self._move_to({(port,i):start},cfg['speed_deg'],'回到起点')
        finally:
            for j,pos in reversed(moved):
                try:self._move_to({(port,j):pos},12,'恢复预置关节')
                except Aborted:raise
                except Exception as exc:self._log(f'{self._label(port,j)}恢复失败：{exc}')
        if 'timeout' in (low['kind'],high['kind']):raise RuntimeError('探测超时，限位未改')
        new_low=low['edge']+(MARGIN_RAW if low['kind']=='stall' else 0)
        new_high=high['edge']-(MARGIN_RAW if high['kind']=='stall' else 0)
        # A stop inside the hand-collected range is posture interference or a
        # torque shortfall: keep the reachable saved end, never shrink it.
        if new_low>old[0]:new_low=old[0];low['kind']='inside'
        if new_high<old[1]:new_high=old[1];high['kind']='inside'
        if not new_low<start<new_high or new_high-new_low<48:raise RuntimeError(f'探测结果无效：{new_low}–{new_high}')
        result={'status':'done','min':new_low,'max':new_high,'min_kind':low['kind'],'max_kind':high['kind'],
                'old_min':old[0],'old_max':old[1],'range_deg':deg(new_high-new_low),'old_range_deg':deg(old[1]-old[0]),
                'peak_load':max(low['peak_load'],high['peak_load']),'applied':False}
        if self.apply:
            self._set_limits(port,i,new_low,new_high);self._update_profile(port,i,low,high,new_low,new_high)
            result['applied']=True
        else:self._set_limits(port,i,*old)
        self.finalized.add(key)
        return result

    def _update_profile(self,port,i,low,high,new_low,new_high):
        reg=self._read(port,i);homing=word(reg,31);key=f'{port}:{i}'
        data=self._profile() or {'schema':3,'kind':'observed_ranges_only','serials':EXPECTED_SERIALS,
                                  'full_manual_range_verified':False,'references_verified':False,'joints':{}}
        if data.get('serials')!=EXPECTED_SERIALS:raise RuntimeError('标定文件身份不符，未更新')
        entry=data.setdefault('joints',{}).get(key) or {}
        if entry.get('homing_raw')!=homing or 'mid' not in entry:entry={'mid':2047,'position':word(reg,56)}
        shift=2047-int(entry['mid'])
        entry.update(min=new_low,max=new_high,homing_raw=homing,range_verified=True,verified=bool(entry.get('verified')),
                     observed_range={'min':low['edge']-shift,'max':high['edge']-shift,'span_raw':high['edge']-low['edge'],
                                     'span_deg':deg(high['edge']-low['edge']),'samples':0},
                     selftest={'at':now(),'min_kind':low['kind'],'max_kind':high['kind'],'margin_raw':MARGIN_RAW})
        data['joints'][key]=entry;atomic_json(self.profile_path,data)

    def _run(self):
        try:
            for item in self.plan:
                self._check_abort();self.current=item;self.results[item]['status']='running'
                try:self.results[item].update(self._run_item(item))
                except Aborted:raise
                except Exception as exc:
                    self.results[item].update(status='failed',message=str(exc))
                    self._log(item+'失败：'+str(exc))
                    if isinstance(exc,OSError):raise
        except Aborted as exc:
            self.error=str(exc);self._log(str(exc))
        except Exception as exc:
            self.error='自检中断：'+str(exc);self._log(self.error)
        finally:
            for item,res in self.results.items():
                if res['status'] in ('queued','running'):res['status']='skipped' if res['status']=='queued' else 'aborted'
            self._cleanup()
            self.running=False;self.current=None;self.finished_at=now()
            try:
                self.results_dir.mkdir(parents=True,exist_ok=True)
                atomic_json(self.results_dir/('limit-selftest-'+time.strftime('%Y%m%d-%H%M%S')+'.json'),
                            {'serials':EXPECTED_SERIALS,'started_at':self.started_at,'finished_at':self.finished_at,
                             'plan':self.plan,'results':self.results,'error':self.error,'events':list(self.events)})
            except OSError:pass

    def _cleanup(self):
        """Hold every touched joint where it is, restore torque ceilings, and
        never leave an unexplored temporary limit: an interrupted joint keeps
        its original range widened only enough to contain where it stopped."""
        r=self.runtime
        for key,(low,high) in list(self.limit_backup.items()):
            if key in self.finalized:continue
            port,i=key.split(':');i=int(i)
            try:
                pos=word(self._read(port,i),56)
                self._set_limits(port,i,min(low,pos),max(high,pos))
            except Exception as exc:self._log(f'{key}临时限位恢复失败：{exc}')
        for key,value in list(self.torque_backup.items()):
            port,i=key.split(':');i=int(i)
            try:
                with r.io:
                    p=r.session.ports[port];reg=read_registers(p,i)
                    if reg[40]:write_register(p,i,42,word(reg,56),2)
                    write_register(p,i,48,value,2)
            except Exception as exc:self._log(f'{key}扭矩上限恢复失败：{exc}')
        try:
            with r.io:r.scan()
        except Exception:pass
