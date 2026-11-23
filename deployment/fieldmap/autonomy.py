"""Vehicle-local exploration supervision, separate from unvalidated navigation.

The deployed inputs do NOT yet contain a live metric navigation backend. This
runner can decide, collect a stationary survey, wait/recover sources and report
without a model. It cannot turn replay maps or photos into wheel clearance.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
import os
import shutil
from pathlib import Path
import time
import uuid

from deployment.grasp.recovery import hardware_readiness, wheel_observation
from .store import stamp
from .phone_source import phone_observation


def replace_json(path,value):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
        for delay in (0,.005,.01,.02,.04,.08):
            if delay:time.sleep(delay)
            try:os.replace(temporary,path);return
            except PermissionError:
                if delay==.08:raise
    finally:temporary.unlink(missing_ok=True)


def anchor(state):
    """Head motion is ours; all other movement invalidates this stationary epoch."""
    positions={f'{p["port"]}:{m["id"]}':m['position'] for p in state['ports'] for m in p['motors']
               if not (p['port']=='COM3' and m['id'] in (7,8))}
    return {'controller_pid':state['server_pid'],'positions':positions,
            'operator':{k:state.get('operator',{}).get(k) for k in ('epoch','seq')}}


def changed_anchor(previous,current):
    if previous['controller_pid']!=current['controller_pid']:return 'controller_restarted'
    if previous['operator']!=current['operator']:return 'operator_reference_changed'
    old,new=previous['positions'],current['positions']
    if old.keys()!=new.keys():return 'motor_topology_changed'
    if any(abs((new[k]-old[k]+2048)%4096-2048)>3 for k in old):return 'uncommanded_pose_change'
    return None


def anchor_delta(previous,current):
    """Signed encoder change per motor since the reference, kept with the stop record."""
    if not previous:return {}
    old,new=previous['positions'],current['positions']
    return {k:(new[k]-old[k]+2048)%4096-2048 for k in sorted(old.keys()&new.keys()) if new[k]!=old[k]}


def decision(observation,scanned):
    """Only local observations select the next action; no model approvals."""
    if observation.get('hard_stop'):return 'latched_stop',[observation['hard_stop']]
    if not observation['hardware_ready']:return 'waiting_hardware',observation['hardware_issues']
    if not observation['sources_fresh']:return 'waiting_source',observation['source_issues']
    if not scanned:return 'survey_station',[]
    # This is deliberate capability reporting, not an operator-clearance switch.
    return 'waiting_navigation',observation['navigation_blockers']


class StudioInputs:
    """Read original local services. HTTP bodies cannot supply readiness flags."""
    def __init__(self,app,scan):
        self.app=app;self.scan=scan;self.history=[]

    async def __call__(self):
        state=await self.scan.api('/api/status')
        servo=await self.scan.api('/api/servo/status')
        row=wheel_observation(state)
        if row['at'] is not None:
            self.history=[r for r in self.history if 0<=row['at']-r['at']<=2.5 and r['at']!=row['at']]+[row]
        health=hardware_readiness(state,parked_history=self.history)
        hard=None
        if state.get('web_control',{}).get('active') or state.get('operator',{}).get('active') or state.get('controller',{}).get('controller')!='offline':
            hard='operator_takeover'
        if self.scan.busy():hard='other_robot_operation'
        running=bool(self.scan.task and not self.scan.task.done())
        owns_agent=self.scan.agent_id is not None and state.get('agent_run',{}).get('run_id')==self.scan.agent_id
        owns_servo=(self.scan.servo_id is not None and servo.get('session_id')==self.scan.servo_id) or bool(
            self.scan.state.get('pending_servo_start') and self.scan.state.get('native_servo_label') and
            servo.get('label')==self.scan.state['native_servo_label'])
        # ServoSession shares LimitSelfTest's execution mutex. Its `running`
        # bit alone does not identify another self-test.
        limits=state.get('limit_selftest',{})
        current=limits.get('current') or ''
        own_mutex=running and ((owns_servo and current=='servo') or
                              (owns_agent and (current=='agent' or current.startswith('agent:'))))
        if limits.get('running') and not own_mutex:hard='other_robot_operation'
        if state.get('agent_run',{}).get('running') and not (running and owns_agent):hard='foreign_agent_operation'
        if (servo.get('active') or servo.get('busy')) and not (running and owns_servo):hard='foreign_servo_operation'
        source_issues=[];frames=[]
        try:
            values=await self.scan._views()
            frames=[dict(meta) for _,meta in values.values()]
        except Exception as exc:source_issues.append(str(exc)[:250])
        perception=getattr(self.app.state,'perception',None)
        p=await asyncio.to_thread(perception.status) if perception else {}
        phone=getattr(self.app.state,'phone_vision',None)
        phones=await asyncio.to_thread(phone.status) if phone else {}
        blockers=['continuous_localization_not_connected','sensor_to_base_extrinsics_unverified',
                  'body_footprint_not_measured','live_navigation_backend_not_connected']
        if p.get('calibration',{}).get('status')!='adopted':blockers.insert(0,'metric_ground_clearance_unvalidated')
        return dict(state=state,anchor=anchor(state),hardware_ready=health['ready'],
                    hardware_issues=health['issues'] or ([] if health['ready'] else ['parking_window_not_yet_verified']),
                    sources_fresh=not source_issues,source_issues=source_issues,frames=frames,hard_stop=hard,
                    navigation_blockers=blockers,live_navigation_connected=False,
                    ownership=dict(limit_running=limits.get('running'),limit_current=current,
                                   servo_session=servo.get('session_id'),owns_servo=owns_servo,owns_agent=owns_agent),
                    phone=phone_observation(phones),
                    perception=dict(running=p.get('running',False),calibration=p.get('calibration')))

    def resources(self):
        """Local resource observations only; never manufacture clearance."""
        result={'issues':[]}
        free=shutil.disk_usage(self.scan.root).free
        result['free_bytes']=free
        if free<512*1024**2:result['issues'].append('evidence_storage_low')
        if os.name=='nt':
            import ctypes
            class Power(ctypes.Structure):
                _fields_=[('ac',ctypes.c_ubyte),('flags',ctypes.c_ubyte),('percent',ctypes.c_ubyte),
                          ('reserved',ctypes.c_ubyte),('remaining',ctypes.c_ulong),('full',ctypes.c_ulong)]
            value=Power()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(value)):
                result['power']=dict(ac_line=value.ac,battery_percent=value.percent if value.percent<=100 else None)
                if value.ac==0 and value.percent<=15:result['issues'].append('vehicle_battery_low')
        return result

    def phone_snapshot(self):
        from .phone_source import capture_phone
        return capture_phone(getattr(self.app.state,'phone_vision',None),getattr(self.app.state,'phone_evidence',None))


class AutonomousSurvey:
    def __init__(self,root,exploration,scan,inputs,*,clock=time.monotonic,tick_s=1.):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.exploration=exploration;self.scan=scan;self.inputs=inputs;self.clock=clock;self.tick_s=tick_s
        self.task=None;self.starting=False;self.cancelled=False;self.generation=0;self.wake=asyncio.Event()
        self.state={'phase':'idle','running':False,'event_sequence':0,'events':[]}
        if (self.root/'latest.json').exists():
            self.state=json.loads((self.root/'latest.json').read_text('utf8'))
            if self.state.get('running'):
                self.state.update(phase='interrupted',running=False,hard_hold='restart_requires_review',
                                  note='Studio重启；保留进度与停止原因，不重放动作')
                self._save()

    def _save(self):
        self.state['updated_at']=stamp()
        if self.state.get('id'):replace_json(self.root/self.state['id']/'result.json',self.state)
        replace_json(self.root/'latest.json',self.state)

    def _event(self,kind,**value):
        self.state['event_sequence']+=1
        event=dict(sequence=self.state['event_sequence'],at=stamp(),elapsed_s=round(self.clock()-self.begun,3),kind=kind,**value)
        # Persist the event independently of the dashboard pointer.
        with (self.root/self.state['id']/'events.jsonl').open('a',encoding='utf8') as stream:
            stream.write(json.dumps(event,ensure_ascii=False,allow_nan=False)+'\n')
        self.state['events']=(self.state['events']+[event])[-128:]
        self._save()

    def status(self):
        keys=('id','phase','running','started_at','updated_at','finished_at','elapsed_s','note','blocked_by',
              'hard_hold','error','persistence_error','progress','child_id','event_sequence','last_summary',
              'parked_verified','policy','source_wait_checks','latest_inputs','cancelled','continuous',
              'round','round_elapsed_s','round_started_at','latest_observation','resource_status')
        value={**{k:deepcopy(self.state[k]) for k in keys if k in self.state},
                'execution':'vehicle_local_decisions','llm_calls':0,'model_step_approvals':0,
                'automatic_translation':False,'automatic_rotation':False,
                'navigation_backend':'not_connected','metric_map_completed':False,
                'right_lift_hold_retained':True,'restart_replays_motion':False}
        if self.state.get('running'):value['elapsed_s']=round(self.clock()-self.begun,3)
        value['task_lifetime']='until_stop_or_guard' if self.state.get('continuous') else 'finite'
        return value

    def record(self,identifier):
        if not isinstance(identifier,str) or len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier):
            raise ValueError('无效的自主任务ID')
        return json.loads((self.root/identifier/'result.json').read_text('utf8'))

    async def start(self,value):
        if self.starting or (self.task and not self.task.done()):raise ValueError('自主任务正在执行或预检')
        allowed={'confirmed','authorization','mission_id','max_duration_s','report_interval_s','no_progress_s','continuous'}
        if set(value)-allowed or value.get('confirmed') is not True or not isinstance(value.get('authorization'),str) or not 8<=len(value['authorization'])<=1000:
            raise ValueError('需要自主任务授权；不接收逐段目标或放行开关')
        if type(value.get('continuous',False)) is not bool:raise ValueError('持续任务必须明确true或false')
        policy={k:value.get(k,default) for k,default in (('max_duration_s',900),('report_interval_s',60),('no_progress_s',180))}
        for key,low,high in (('max_duration_s',60,3600),('report_interval_s',30,300),('no_progress_s',60,600)):
            if type(policy[key]) not in (int,float) or not math.isfinite(policy[key]) or not low<=policy[key]<=high:
                raise ValueError('无效的时间预算：'+key)
        if self.state.get('hard_hold'):raise ValueError('保留的停止锁尚未处理：'+self.state['hard_hold'])
        mission=self.exploration.status()
        if not mission['enabled'] or mission['session_id']!=value.get('mission_id'):raise ValueError('探索会话已停止或变化')
        if self.scan.state.get('phase') in ('needs_review','interrupted'):
            raise ValueError('先处理原本地任务未决故障，不以新任务清除')
        self.starting=True;generation=self.generation
        identifier=uuid.uuid4().hex
        try:
            self.scan.reserve(identifier)
            state,_,_=await self.scan.parked()
            latest=self.exploration.status()
            if generation!=self.generation or not latest['enabled'] or latest['session_id']!=mission['session_id']:
                raise ValueError('预检期间收到停止或会话改变')
            self.cancelled=False;self.wake.clear();self.begun=self.clock();self.last_progress=self.begun
            self.next_report=self.begun+policy['report_interval_s'];self.no_progress_reported=False
            self.preflight_after=0.;self.preflight_failures=0
            self.heartbeat_after=0.;self.heartbeat_phase=None
            self.station_scanned=False;self.base_anchor=anchor(state)
            self.round_begun=self.begun;self.observation_after=self.begun;self.resource_after=self.begun
            (self.root/identifier).mkdir()
            self.state=dict(id=identifier,phase='starting',running=True,mission_id=mission['session_id'],
                            authorization=value['authorization'],policy=policy,started_at=stamp(),elapsed_s=0,
                            progress=dict(station_scans=0,views=0,translation_legs=0,new_area_m2=None),
                            continuous=value.get('continuous',False),round=1,round_elapsed_s=0,round_started_at=stamp(),
                            event_sequence=0,events=[],blocked_by=[],source_wait_checks=0,parked_verified=True,
                            physical_holds=['right_lift_existing_hold'])
            self._event('started',policy=policy)
            self.task=asyncio.create_task(self._run(),name='fieldmap-local-autonomy')
            return self.status()
        except BaseException as exc:
            if self.state.get('id')==identifier:
                self.state.update(running=False,phase='latched_stop',hard_hold='start_failed',error=str(exc))
                try:self._save()
                except OSError as failure:self.state['persistence_error']=str(failure)
            self.scan.release(identifier);raise
        finally:self.starting=False

    def _latch(self,reason,**evidence):
        self.state.update(phase='latched_stop',hard_hold=reason,note=reason,blocked_by=[reason])
        # Later readbacks cannot show which motor differed at the moment the lock was set.
        self._event('intervention_required',reason=reason,**evidence)

    def _mission(self):
        mission=self.exploration.status()
        return mission if mission['enabled'] and mission['session_id']==self.state['mission_id'] else None

    def _heartbeat(self):
        if self.clock()<self.heartbeat_after and self.heartbeat_phase==self.state['phase']:return
        m=self._mission()
        if m:
            self.exploration.update(m['session_id'],'mapping' if self.state['phase']=='survey_station' else 'guarded_stop',
                '车载自主决策：'+self.state['phase']+'；'+','.join(self.state.get('blocked_by',[])),
                **{k:m[k] for k in ('translation_legs','turn_attempts','wheel_path_m')})
            self.heartbeat_after=self.clock()+15;self.heartbeat_phase=self.state['phase']

    async def _tick(self):
        try:observation=await self.inputs()
        except Exception as exc:
            if self.scan.task and not self.scan.task.done():
                self._latch('feedback_unknown_during_action');await self.scan.stop('自主反馈未知');return
            self._transition('waiting_source',['local_input_unavailable:'+str(exc)[:160]])
            return
        self.state['latest_inputs']={k:deepcopy(observation[k]) for k in ('phone','perception','ownership','live_navigation_connected') if k in observation}
        reason=observation.get('hard_stop') or changed_anchor(self.base_anchor,observation['anchor'])
        if reason:
            self._latch(reason,reference_delta=anchor_delta(self.base_anchor,observation['anchor']))
            await self.scan.stop('自主任务停止：'+reason);return
        if self.state.get('child_id'):
            child=self.scan.status()
            if child.get('id')==self.state['child_id']:
                views=child.get('completed_views',0);previous=self.state.get('child_registered_views',0)
                if views>previous:
                    self.state['progress']['views']+=views-previous;self.state['child_registered_views']=views
                    self.last_progress=self.clock();self.no_progress_reported=False
                    self._event('observation_progress',job_id=child['id'],views=views)
            if self.scan.task and not self.scan.task.done():
                self._transition('waiting_source' if child['phase']=='waiting_source' else 'survey_station',
                                 ['head_source_recovery'] if child['phase']=='waiting_source' else [])
                return
            if child.get('id')!=self.state['child_id'] or child.get('phase')!='completed' or not child.get('parked_verified') or child.get('review_required'):
                self._latch('station_action_failed_or_uncertain');return
            self.state['progress']['station_scans']+=1;self.station_scanned=True
            self.state['completed_child_id']=self.state.pop('child_id')
            self.last_progress=self.clock();self.no_progress_reported=False
            self._event('station_survey_completed',job_id=child['id'],views=child['completed_views'],atlas_id=child.get('atlas_id'))
        if self.state.get('continuous') and self.clock()>=self.resource_after:
            provider=getattr(self.inputs,'resources',None)
            if provider:
                try:self.state['resource_status']=await asyncio.to_thread(provider)
                except Exception as exc:self.state['resource_status']=dict(issues=['resource_read_unavailable'],error=str(exc)[:200])
            self.resource_after=self.clock()+30
        resources=self.state.get('resource_status',{})
        if resources.get('issues'):
            self._transition('waiting_resources',resources['issues']);return
        phase,reasons=decision(observation,self.station_scanned)
        self._transition(phase,reasons)
        if phase=='latched_stop':self._latch(reasons[0]);return
        if phase=='waiting_source':self.state['source_wait_checks']+=1
        if self.state.get('continuous') and phase=='waiting_navigation' and self.clock()>=self.observation_after:
            await self._observation_snapshot(observation)
        if phase=='survey_station':
            if self.clock()<self.preflight_after:return
            # Both observation creation and the decision to scan happen locally.
            generation=self.generation
            try:
                review=await self.scan.review(owner=self.state['id'])
                if self.cancelled or generation!=self.generation or not self._mission():return
                self._event('local_action_selected',action='head_layers',reason='fresh_station_survey',model_approval=False)
                child=await self.scan.start(dict(confirmed=True,authorization=self.state['authorization'],profile='head_layers',
                    mission_id=self.state['mission_id'],review_id=review['review_id']),owner=self.state['id'])
            except ValueError as exc:
                # These are executor preflight rejections, not physical retries.
                self.preflight_failures+=1;self.preflight_after=self.clock()+min(10,2*self.preflight_failures)
                self._event('preflight_deferred',reason=str(exc),rejections=self.preflight_failures,motion_attempts=0)
                if self.preflight_failures>=10:self._latch('local_preflight_recovery_exhausted')
                return
            self.state['child_id']=child['id'];self.state['child_registered_views']=0;self._save()

    async def _observation_snapshot(self,observation):
        """One parked source sample per summary; no new coverage or motor grant."""
        self.observation_after=self.clock()+self.state['policy']['report_interval_s']
        identifier=uuid.uuid4().hex;folder=self.root/self.state['id']/'observations'/identifier
        folder.mkdir(parents=True)
        import hashlib
        record=dict(id=identifier,at=stamp(),round=self.state['round'],motor_commands=0,
                    adds_explored_area=False,phone=deepcopy(observation.get('phone')),photos=[])
        for camera,(raw,meta) in self.scan.frames.items():
            name=f'camera-{camera}.jpg';(folder/name).write_bytes(raw)
            record['photos'].append(dict(meta,file=name,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
        provider=getattr(self.inputs,'phone_snapshot',None)
        if provider:
            try:record['phone_capture']=await asyncio.to_thread(provider)
            except Exception as exc:record['phone_error']=str(exc)[:200]
        replace_json(folder/'record.json',record)
        self.state['latest_observation']={k:deepcopy(record[k]) for k in ('id','at','round','motor_commands','adds_explored_area','photos')}
        self._event('parked_observation_saved',observation_id=identifier,new_area_m2=None)

    async def _round_boundary(self):
        # The same parent, reservation, stop generation and station reference
        # survive a round. A new round never replays a completed child.
        if self.scan.task and not self.scan.task.done():return
        generation=self.generation
        state,_,_=await self.scan.parked()
        if self.cancelled or generation!=self.generation or not self._mission():return
        current=anchor(state);reason=changed_anchor(self.base_anchor,current)
        if reason:self._latch(reason,reference_delta=anchor_delta(self.base_anchor,current));return
        self.state['parked_verified']=True
        self._event('round_completed',round=self.state['round'],station_reference_retained=True,
                    new_motor_permissions=False,progress=deepcopy(self.state['progress']))
        self.round_begun=self.clock();self.state.update(round=self.state['round']+1,round_started_at=stamp(),round_elapsed_s=0)
        self._save()

    def _transition(self,phase,reasons):
        if phase!=self.state['phase'] or reasons!=self.state.get('blocked_by'):
            before=self.state['phase'];self.state.update(phase=phase,blocked_by=reasons)
            self.state['note']='本地等待条件恢复，保留任务' if phase.startswith('waiting_') else '本地选择并执行驻点补扫'
            self._event('state_changed',previous=before,phase=phase,reasons=reasons)

    def _reports(self):
        now=self.clock();self.state['elapsed_s']=round(now-self.begun,3)
        if now>=self.next_report:
            summary=dict(phase=self.state['phase'],progress=deepcopy(self.state['progress']),
                         blocked_by=self.state.get('blocked_by',[]),since_progress_s=round(now-self.last_progress,3))
            self.state['last_summary']=summary;self._event('periodic_summary',**summary)
            self.next_report=now+self.state['policy']['report_interval_s']
        if not self.no_progress_reported and now-self.last_progress>=self.state['policy']['no_progress_s']:
            self.no_progress_reported=True
            self._event('no_progress',since_progress_s=round(now-self.last_progress,3),
                        reasons=self.state.get('blocked_by',[]),task_retained=True)

    async def _run(self):
        try:
            while not self.cancelled and self._mission():
                if self.state.get('continuous'):
                    self.state['round_elapsed_s']=round(self.clock()-self.round_begun,3)
                    if self.clock()-self.round_begun>=self.state['policy']['max_duration_s']:
                        await self._round_boundary()
                        if self.cancelled or not self._mission() or self.state.get('hard_hold'):break
                elif self.clock()-self.begun>=self.state['policy']['max_duration_s']:
                    self.state.update(phase='budget_reached',note='本地时间预算到期，保留未完成任务；未宣告建图完成')
                    self._event('time_budget_reached',mapping_completed=False);break
                await self._tick()
                self._reports();self._heartbeat()
                if self.state.get('completed_task'):break
                if self.state.get('hard_hold'):break
                try:await asyncio.wait_for(self.wake.wait(),timeout=self.tick_s)
                except asyncio.TimeoutError:pass
        except (Exception,asyncio.CancelledError) as exc:
            self.state.update(phase='latched_stop',hard_hold='local_task_error',error=str(exc))
        finally:
            # A logging failure must never skip the original owned stop paths.
            try:
                if self.scan.reserved_by==self.state.get('id') and (self.scan.starting or (self.scan.task and not self.scan.task.done())):
                    try:await self.scan.stop('自主任务退出')
                    except Exception as exc:self.state['stop_error']=str(exc)
                    if self.scan.task and not self.scan.task.done():
                        try:await asyncio.wait_for(asyncio.shield(self.scan.task),timeout=40)
                        except (Exception,asyncio.CancelledError) as exc:
                            self.state.update(stop_error=str(exc),hard_hold='owned_action_not_terminated')
            finally:
                try:
                    _,_,_=await self.scan.parked();self.state['parked_verified']=True
                except Exception as exc:self.state.update(parked_verified=False,parking_error=str(exc),hard_hold='parking_not_verified')
                if self.cancelled or not self._mission():self.state.update(phase='stopped',cancelled=True,note='任务已停止，不自动恢复')
                self.state.update(running=False,finished_at=stamp(),elapsed_s=round(self.clock()-self.begun,3))
                try:self._save()
                except OSError as exc:self.state['persistence_error']=str(exc)
                self.scan.release(self.state['id'])

    async def stop(self,reason='用户停止自主任务'):
        self.generation+=1;self.cancelled=True;self.wake.set()
        self.state.update(cancelled=True,note=reason)
        if self.state.get('running'):self.state['phase']='stopping'
        try:self._save()
        except OSError as exc:self.state['persistence_error']=str(exc)
        if self.scan.reserved_by==self.state.get('id') and (self.scan.starting or (self.scan.task and not self.scan.task.done())):
            await self.scan.stop(reason)
        return self.status()

    async def close(self):
        if self.task and not self.task.done():
            await self.stop('Studio退出')
            try:await asyncio.wait_for(asyncio.shield(self.task),timeout=45)
            except asyncio.TimeoutError:
                self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
