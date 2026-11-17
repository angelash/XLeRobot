"""Finite local observation jobs in Studio; no LLM, serial or translation.

Head layers run in one stationary-base servo session. Reviewed turn batches
reuse the existing timed/feedback/stopping primitive in 8765. A completed job
returns evidence for the supervisor; it never silently starts the next batch.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import time
import uuid

import httpx
from fastapi import HTTPException

from deployment.grasp.recovery import hardware_readiness
from .store import atomic_json, stamp

ACTIVE = {'starting', 'observing', 'moving', 'waiting_source', 'saving', 'stopping'}


def head_path(origin, target, maximum=360):
    """Split before sending: the original servo API rejects oversized steps."""
    count=max(1, math.ceil(max(abs(target[k]-origin[k]) for k in target)/maximum))
    return [{k:round(origin[k]+(target[k]-origin[k])*i/count) for k in target}
            for i in range(1,count+1)]


def measured_head(entry):
    values={key:(entry.get('joints',{}).get(key) or {}).get('position') for key in ('head:7','head:8')}
    if any(type(v) is not int or not 0<=v<4096 for v in values.values()):
        raise RuntimeError('原头部动作回执缺少实际位置，不能用请求目标替代')
    return values


class LocalScan:
    def __init__(self, root, exploration, camera, phone=None, busy=None, client=None, publish=None):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.exploration=exploration;self.camera=camera;self.phone=phone;self.busy=busy or (lambda:False)
        self.publish=publish
        self.client=client or httpx.AsyncClient(base_url='http://127.0.0.1:8765',trust_env=False,timeout=3)
        self.own_client=client is None;self.task=None;self.cancelled=False
        self.stop_generation=0
        self.state={'phase':'idle','running':False};self.frames={};self.servo_id=None;self.agent_id=None
        self.stop_reason=None;self.phone_epoch=None;self.phone_token=None
        # Restart is an interruption, never permission to replay an old queue.
        pointer=self.root/'latest.json'
        if pointer.exists():
            self.state=json.loads(pointer.read_text('utf8'))
            if self.state.get('phase') in ACTIVE:
                self.state.update(phase='interrupted',running=False,review_required=True,
                                  note='Studio重启，旧动作队列不自动重放')
                self._save()

    def _save(self):
        self.state['updated_at']=stamp()
        atomic_json(self.root/'latest.json',self.state)
        if self.state.get('id'):atomic_json(self.root/self.state['id']/'result.json',self.state)

    def status(self):
        keys=('id','phase','running','profile','planned_views','completed_views','native_run_id',
              'planned_pulses','registered_pulses','passed_pulses','elapsed_s','timing_s',
              'source_waits','phone_epochs','phone_samples','phone_gaps','error','note',
              'review_required','parked_verified','atlas_id','cancelled','started_at','updated_at')
        result={k:deepcopy(self.state[k]) for k in keys if k in self.state}
        result.update(execution='vehicle_local_finite_job',llm_calls=0,translation_enabled=False,
                      metric_map_verified=False,right_lift_hold_retained=True)
        return result

    async def api(self,path,body=None,timeout=4):
        response=await self.client.request('GET' if body is None else 'POST',path,json=body,timeout=timeout)
        response.raise_for_status();return response.json()

    async def parked(self):
        # Reuse the exact existing complete stationary window and topology check.
        snapshots=[];until=time.monotonic()+2.5
        while True:
            state=await self.api('/api/status');snapshots.append(state)
            from deployment.grasp.recovery import wheel_observation
            rows=[wheel_observation(s) for s in snapshots]
            rows=list({r['at']:r for r in rows if r['at'] is not None}.values())
            if (len(rows)>=3 and rows[-1]['at']-rows[0]['at']>=.6) or time.monotonic()>=until:break
            await asyncio.sleep(.12)
        health=hardware_readiness(state,parked_history=rows)
        if not health['ready'] or not health['stationary_base_verified']:
            raise RuntimeError('完整硬件／停车窗未通过：'+','.join(health['issues']))
        return state,rows,health

    def _latch(self):
        mission=self.exploration.status()
        if self.cancelled or not mission['enabled'] or mission['session_id']!=self.state['mission_id']:
            raise RuntimeError('任务已停止或参考会话已改变')
        return mission

    def _heartbeat(self,phase='mapping'):
        m=self._latch()
        self.exploration.update(m['session_id'],phase,
            f'本地批次 {self.state["profile"]}：{self.state.get("completed_views",0)}张，零大模型调用',
            **{k:m[k] for k in ('translation_legs','turn_attempts','wheel_path_m')})

    async def _views(self):
        # A short network timeout never turns the reception time into source age.
        values=await asyncio.gather(*(self.camera(c) for c in (0,1,2)))
        fresh={}
        for c,(raw,headers) in zip((0,1,2),values):
            at=float(headers['X-Captured-At']);age=time.time()-at
            if not -.1<=age<=.8 or not raw.startswith(b'\xff\xd8'):
                raise RuntimeError('机器人相机缺少原800ms内的新图')
            fresh[c]=(raw,{'camera':c,'captured_at':at,'sequence':int(headers['X-Frame-Sequence']),
                           'sha256':hashlib.sha256(raw).hexdigest()})
        self.frames=fresh
        return fresh

    async def _wait_views(self,seconds=8):
        begun=time.monotonic();last=None;logged=False
        while True:
            self._latch()
            try:
                frames=await self._views()
                if logged:
                    self.state['source_waits'][-1]['recovered']=True
                    self.state['source_waits'][-1]['elapsed_s']=round(time.monotonic()-begun,3)
                self.state['phase']='observing';self._save();return frames
            except (httpx.HTTPError,HTTPException,RuntimeError,KeyError,ValueError) as exc:
                last=str(exc)
                if not logged:
                    self.state['source_waits'].append({'at':stamp(),'reason':last,'recovered':False});logged=True
                self.state['phase']='waiting_source';self._save()
                if self.servo_id:await self.api('/api/servo/hold',{'session_id':self.servo_id})
                if time.monotonic()-begun>=seconds:raise RuntimeError('相机恢复预算用完：'+last) from exc
                await asyncio.sleep(.2)

    def _photos(self,tag,frames):
        shots=[]
        for c,(raw,meta) in frames.items():
            name=f'{tag}-cam{c}.jpg';(self.root/self.state['id']/name).write_bytes(raw)
            shots.append({**meta,'file':name,'bytes':len(raw)})
        return shots

    async def review(self):
        if self.task and not self.task.done():raise ValueError('本地批次正在执行')
        s,rows,health=await self.parked()
        servo=await self.api('/api/servo/status')
        if s['agent_run']['running'] or servo['active'] or servo['busy'] or s['web_control']['active'] or s['controller']['controller']!='offline' or self.busy():
            raise ValueError('其他执行器／操作员占用中')
        frames=await self._views();identifier=uuid.uuid4().hex
        folder=self.root/identifier;folder.mkdir()
        photos=[]
        for c,(raw,meta) in frames.items():
            name=f'anchor-cam{c}.jpg';(folder/name).write_bytes(raw);photos.append({**meta,'file':name})
        review={'id':identifier,'at':time.time(),'photos':photos,'state':s,'parked_history':rows,'health':health}
        atomic_json(folder/'review.json',review)
        return {'review_id':identifier,'photos':photos,'expires_in_s':90,'motion_commands':0}

    async def start(self,value):
        generation=self.stop_generation
        if self.task and not self.task.done():raise ValueError('本地批次正在执行')
        allowed={'confirmed','authorization','profile','mission_id','review_id','pulses','direction'}
        if set(value)-allowed or value.get('confirmed') is not True or not isinstance(value.get('authorization'),str) or not 8<=len(value['authorization'])<=1000:
            raise ValueError('需要本次有限批次的明确授权')
        profile=value.get('profile')
        if profile not in ('head_layers','turn_batch'):raise ValueError('只支持驻点分层补扫或有限旋转批次')
        mission=self.exploration.status()
        if not mission['enabled'] or value.get('mission_id')!=mission['session_id']:raise ValueError('探索会话已停止或改变')
        review_id=value.get('review_id','')
        if not isinstance(review_id,str) or len(review_id)!=32 or any(c not in '0123456789abcdef' for c in review_id):raise ValueError('需要本次停车观察依据')
        review=json.loads((self.root/review_id/'review.json').read_text('utf8'))
        if not 0<=time.time()-review['at']<=90:raise ValueError('观察依据已过期，请重新观察')
        pulses=value.get('pulses',12);direction=value.get('direction',1)
        if type(pulses) is not int or not 1<=pulses<=12 or type(direction) is not int or direction not in (-1,1):raise ValueError('单批最多12个原650ms／raw160旋转段')
        state,history,health=await self.parked();servo=await self.api('/api/servo/status')
        if state['server_pid']!=review['state']['server_pid']:raise ValueError('控制器已更换，需要重新观察')
        def positions(s):return {f'{p["port"]}:{m["id"]}':m['position'] for p in s['ports'] for m in p['motors']}
        current=positions(state);previous=positions(review['state'])
        if set(current)!=set(previous) or any(abs((current[k]-previous[k]+2048)%4096-2048)>3 for k in current):raise ValueError('观察后姿态已变化，需要新参考')
        if state['agent_run']['running'] or servo['active'] or servo['busy'] or state['web_control']['active'] or state['controller']['controller']!='offline' or self.busy():raise ValueError('其他执行器／操作员占用中')
        if profile=='turn_batch':
            for p in ('COM3','COM4'):
                if not(current[p+':2']<1350 and current[p+':3']>2800 and current[p+':6']<1400 and 1800<current[p+':1']<2250):raise ValueError('收臂包络已变化，不能旋转')
        if generation!=self.stop_generation:raise ValueError('预检期间收到停止，不启动排队动作')
        latest=self.exploration.status()
        if not latest['enabled'] or latest['session_id']!=mission['session_id']:raise ValueError('预检期间探索已停止或改变')
        identifier=uuid.uuid4().hex;(self.root/identifier).mkdir()
        self.cancelled=False;self.stop_reason=None;self.servo_id=None;self.agent_id=None
        self.phone_epoch=None;self.phone_token=None;self.frames={}
        self.state=dict(id=identifier,phase='starting',running=True,profile=profile,mission_id=mission['session_id'],
            authorization=value['authorization'],review_id=review_id,started_at=stamp(),completed_views=0,
            planned_views=18 if profile=='head_layers' else pulses,planned_pulses=pulses if profile=='turn_batch' else 0,
            registered_pulses=0,passed_pulses=0,source_waits=[],phone_epochs=[],phone_samples=0,phone_gaps=0,
            observations=[],physical_steps=[],timing_s={},initial_state=state,initial_parked_history=history,
            health=health,right_lift_hold_retained=True,llm_calls=0,translation_commands=0,review_required=False)
        self._save()
        self.task=asyncio.create_task(asyncio.wait_for(self._run(direction),timeout=180),name='fieldmap-local-scan')
        return self.status()

    async def _phone_once(self):
        if self.phone is None:return
        try:
            sample=await asyncio.to_thread(self.phone)
            if sample is None:
                self.state['phone_gaps']+=1;return
            token=(sample['session'],sample['sequence'])
            if token==self.phone_token:return
            if token[0]!=self.phone_epoch:
                self.state['phone_epochs'].append({'session':token[0],'first_sequence':token[1],
                    'registered_with_previous':False,'first_at':stamp()})
                self.phone_epoch=token[0]
            self.phone_token=token;self.state['phone_samples']+=1
            self.state.setdefault('phone_captures',[]).append(sample)
        except Exception as exc:
            self.state['phone_gaps']+=1;self.state['last_phone_error']=str(exc)[:200]

    async def _watch(self):
        while True:
            self._latch()
            # No model, SSH or full-state scan in the camera watchdog.
            await self._views()
            await asyncio.sleep(.18)

    async def _guarded(self,operation):
        motion=asyncio.create_task(operation);watch=asyncio.create_task(self._watch())
        try:
            done,_=await asyncio.wait((motion,watch),return_when=asyncio.FIRST_COMPLETED)
            if watch in done:
                await self._stop_native('本地源龄／任务守卫停止')
                # Preserve the original request's result; never resend on timeout.
                try:self.state.setdefault('interrupted_receipts',[]).append(await motion)
                except Exception as exc:self.state['uncertain_action_error']=str(exc)
                watch.result()
            return await motion
        finally:
            watch.cancel();await asyncio.gather(watch,return_exceptions=True)
            if not motion.done():
                # Cancellation also stops the awaited native operation; never
                # leave a detached step to finish after the job's final receipt.
                try:await self._stop_native('本地任务退出，核对未完成原动作')
                except Exception as exc:self.state['stop_error']=str(exc)
                try:self.state.setdefault('interrupted_receipts',[]).append(await asyncio.wait_for(asyncio.shield(motion),30))
                except Exception as exc:self.state['uncertain_action_error']=str(exc)

    async def _head(self):
        begin=await self.api('/api/servo/session',{'confirmed':True,'label':'local station height layers',
            'lease_s':4.,'stationary_base':True},timeout=20)
        self.servo_id=begin['session_id'];self.state['native_servo_session']=self.servo_id;self._save()
        if not begin.get('stationary_base') or not begin.get('wheel_park',{}).get('verified'):raise RuntimeError('原会话未认证底盘停车')
        initial=self.state['initial_state']
        origin={f'head:{m["id"]}':m['position'] for p in initial['ports'] if p['port']=='COM3' for m in p['motors'] if m['id'] in (7,8)}
        current=dict(origin);pans=[1230,1540,1850,2160,2470,2787]
        targets=[{'head:7':pan,'head:8':tilt} for i,tilt in enumerate((2150,2450,2750)) for pan in (pans if i%2==0 else list(reversed(pans)))]
        for target in targets+[origin]:
            self._heartbeat();await self._wait_views()
            for part in head_path(current,target):
                self._latch();self.state['phase']='moving';self.state['pending_target']=part;self._save()
                entry=await self._guarded(self.api('/api/servo/step',dict(session_id=self.servo_id,targets=part,
                    torque=450,speed_deg=18,label='local head layer'),timeout=25))
                self.state['physical_steps'].append(entry);self._save()
                if entry.get('contact') or entry.get('ended'):raise RuntimeError('头部受阻或会话结束，保留现场，不自动重发')
                current=measured_head(entry)
            if target is origin:break
            # Save a source frame newer than the completed movement, not its preview.
            settled_at=time.time();until=time.monotonic()+2
            while True:
                frames=await self._wait_views(seconds=2)
                if frames[0][1]['captured_at']>=settled_at:break
                if time.monotonic()>=until:raise RuntimeError('动作后缺少新头图')
                await asyncio.sleep(.06)
            shots=self._photos(f'view-{self.state["completed_views"]:02d}',{0:frames[0]})
            self.state['observations'].append(dict(head_raw=[current['head:7'],current['head:8']],
                requested_head_raw=[target['head:7'],target['head:8']],joint_readback_at=entry.get('at'),shots=shots,
                heading_deg=(current['head:7']-2015)*360/4096,pitch_deg=-(current['head:8']-2220)*360/4096))
            self.state['completed_views']+=1
            await self._phone_once();self._save()

    def _native_photos(self):
        receipt=self.state.get('native_receipt',{})
        if not receipt.get('dir'):return
        evidence=Path(__file__).resolve().parents[2]/'fabrication/assembly/evidence'
        source=Path(receipt['dir']).resolve()
        if not source.is_relative_to(evidence.resolve()):raise RuntimeError('原照片目录不在控制器证据范围内')
        for row in receipt.get('results',[]):
            if row['op']!='snapshot' or row['status']!='done':continue
            shots=[]
            for shot in row.get('shots',[]):
                if 'file' not in shot:continue
                name=shot['file']
                if Path(name).name!=name or not name.endswith('.jpg'):raise RuntimeError('原照片文件名无效')
                raw=(source/name).read_bytes();target='native-'+name
                (self.root/self.state['id']/target).write_bytes(raw)
                shots.append({**shot,'file':target,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)})
            self.state['observations'].append({'shots':shots,'positions':row.get('positions'),
                'wheel_positions':row.get('wheel_positions'),'capture_started_at':row.get('capture_started_at')})
        self.state['completed_views']=len(self.state['observations'])

    async def _turn(self,direction):
        await self._wait_views();self._heartbeat('moving')
        steps=[]
        for i in range(self.state['planned_pulses']):
            steps.extend([dict(op='rotate',direction=direction,duration=.65,max_raw=160),
                          dict(op='snapshot',cameras=[0,1,2],tag=f'local-{i:02d}')])
        # Record uncertain submissions; HTTP errors never trigger an automatic replay.
        self.state['pending_submission']=True;self._save()
        label='local scan '+self.state['id'];self.state['native_label']=label;self._save()
        try:response=await self.api('/api/agent/run',dict(steps=steps,confirmed=True,label=label),timeout=20)
        except Exception:
            try:self.state['native_receipt']=await self.api('/api/agent/status')
            finally:raise
        self.agent_id=response['run_id'];self.state['native_run_id']=self.agent_id;self._save()
        async def collect():
            until=time.monotonic()+35
            while True:
                self._latch();receipt=await self.api('/api/agent/status')
                if receipt['run_id']!=self.agent_id:raise RuntimeError('原运行ID变化，不能采用他人回执')
                self.state['native_receipt']=receipt
                rotations=[r for r in receipt['results'] if r['op']=='rotate']
                self.state['registered_pulses']=len(rotations)
                self.state['passed_pulses']=sum(r['status']=='done' for r in rotations)
                self._save()
                if not receipt['running']:
                    if receipt.get('error'):raise RuntimeError(receipt['error'])
                    return receipt
                if time.monotonic()>until:raise RuntimeError('本地旋转批次时间预算已到')
                await asyncio.sleep(.18)
        await self._guarded(collect())

    async def _stop_native(self,reason):
        if self.servo_id:
            status=await self.api('/api/servo/status')
            if status.get('session_id')==self.servo_id and status.get('active'):
                await self.api('/api/servo/stop',{'reason':reason})
        elif self.agent_id or self.state.get('pending_submission'):
            status=await self.api('/api/agent/status')
            owns=(status.get('run_id')==self.agent_id if self.agent_id else status.get('label')==self.state.get('native_label'))
            if owns and status.get('running'):await self.api('/api/agent/stop',{'reason':reason})

    async def _run(self,direction):
        begun=time.monotonic()
        try:
            await self._wait_views();self.state['anchor_photos']=self._photos('anchor',self.frames)
            if self.state['profile']=='head_layers':await self._head()
            else:await self._turn(direction)
            self.state['phase']='completed';self.state['note']='本地批次结束，请按汇总决定下一观察位'
        except (Exception,asyncio.CancelledError) as exc:
            self.state.update(phase='cancelled' if self.cancelled else 'needs_review',error=str(exc),review_required=True)
            try:await self._stop_native('本地有限扫描停止')
            except Exception as stop:self.state['stop_error']=str(stop)
        finally:
            if self.servo_id:
                try:await self.api('/api/servo/end',{'session_id':self.servo_id,'reason':'local finite scan ended'})
                except Exception as exc:self.state['end_error']=str(exc)
            # Every failure is durable before any parking, media or atlas work.
            self.state['elapsed_s']=round(time.monotonic()-begun,3);self._save()
            if self.state['profile']=='turn_batch':
                try:self._native_photos()
                except Exception as exc:self.state['media_error']=str(exc);self.state['review_required']=True
            try:
                state,history,health=await self.parked()
                self.state.update(final_state=state,parked_history=history,health=health,parked_verified=True)
            except Exception as exc:
                self.state.update(parked_verified=False,parking_error=str(exc),review_required=True)
                if self.state['phase']=='completed':self.state['phase']='needs_review'
            try:
                await self._views();self.state['final_photos']=self._photos('final',self.frames)
                self._heartbeat('guarded_stop')
            except Exception as exc:self.state['final_observation_error']=str(exc)
            if self.state['phase']=='completed' and self.publish and self.state['profile']=='head_layers':
                try:self.state['atlas_id']=await asyncio.to_thread(self.publish,deepcopy(self.state))
                except Exception as exc:self.state['atlas_error']=str(exc);self.state['review_required']=True
            self.state.update(running=False,finished_at=stamp(),cancelled=self.cancelled,
                elapsed_s=round(time.monotonic()-begun,3));self._save()

    async def stop(self,reason='用户停止本地扫描'):
        # Persist before network I/O; original controller handles physical stop.
        self.stop_generation+=1;self.cancelled=True;self.state.update(cancelled=True,note=reason)
        if self.task and not self.task.done():self.state['phase']='stopping'
        self._save();await self._stop_native(reason)
        return self.status()

    async def close(self):
        if self.task and not self.task.done():
            await self.stop('Studio退出，旧队列不自动恢复')
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        if self.own_client:await self.client.aclose()

    def folder(self,identifier):
        if not isinstance(identifier,str) or len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier):raise ValueError('无效的扫描编号')
        return self.root/identifier

    def read(self,identifier):return json.loads((self.folder(identifier)/'result.json').read_text('utf8'))

    def media(self,identifier,name):
        if not isinstance(name,str) or Path(name).name!=name or not name.endswith('.jpg'):raise ValueError('无效的照片名')
        path=self.folder(identifier)/name
        if not path.is_file():raise ValueError('照片不存在')
        return path
