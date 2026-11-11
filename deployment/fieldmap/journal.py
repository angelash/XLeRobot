"""Passive spatial memory in Studio: inputs, measured outcomes and source images.

Never owns a camera or serial port and never issues motor requests. No retention
cleanup: immutable run files and source captures survive restart and new maps.
"""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
from pathlib import Path
import queue
import threading
import time
import urllib.request
import uuid
from .store import atomic_json


def stamp():return datetime.now().astimezone().isoformat()


class SpatialJournal:
    def __init__(self,root,survey,phone_provider,*,forward_sign=None,clock=time.time):
        self.root=Path(root);self.survey=survey;self.phone_provider=phone_provider
        self.forward_sign=forward_sign;self.clock=clock;self.lock=threading.RLock()
        self.stop_event=threading.Event();self.thread=None;self.pending=queue.Queue(2048)
        self.run=None;self.previous=None;self.last_images=0.;self.last_flush=0.;self.last_map=None
        self.error=None;self.dropped=0;self.sample_count=0;self.images=0;self.depth_captures=0
        self.last_frame={};self.last_event=None;self.event_pid=None;self.last_event_poll=0.;self.settings_path=self.root/'settings.json'
        self.totals_at=0.;self.totals={}

    def enabled(self):
        return json.loads(self.settings_path.read_text('utf8')).get('enabled') is True if self.settings_path.exists() else False

    def configure(self,enabled):
        if type(enabled)is not bool:raise ValueError('需要明确启用或暂停记录')
        self.root.mkdir(parents=True,exist_ok=True);atomic_json(self.settings_path,{'enabled':enabled,'at':stamp(),'delete_existing':False})
        return self.status()

    def status(self):
        if self.clock()-self.totals_at>5:
            values=[]
            for p in self.root.glob('*/run.json'):
                try:values.append(json.loads(p.read_text('utf8')))
                except (OSError,ValueError):continue
            self.totals={'retained_runs':len(values),'retained_samples':sum(v.get('samples',0) for v in values),
                         'retained_images':sum(len(v.get('photos',[])) for v in values),
                         'retained_depth_frames':sum(v.get('depth_frames',0) for v in values),
                         'retained_input_events':sum(v.get('input_events',0) for v in values)};self.totals_at=self.clock()
        return {'enabled':self.enabled(),'running':bool(self.thread and self.thread.is_alive()),
                **self.totals,
                'sample_interval_s':.5,'sample_count':self.sample_count,'image_count':self.images,
                'depth_capture_count':self.depth_captures,'dropped_input_events':self.dropped,
                'last_sample_at':self.previous.get('at') if self.previous else None,
                'current_run':self.run['id'] if self.run else None,'error':self.error,
                'retention':'append_only_no_automatic_deletion','exact_inputs':'accepted_Studio_web_requests',
                'other_inputs':'sampled_controller_state_and_events','motion_enabled':False}

    def start(self):
        if self.thread and self.thread.is_alive():return
        if not self.enabled():return
        self.root.mkdir(parents=True,exist_ok=True);self.stop_event.clear()
        self.thread=threading.Thread(target=self._loop,daemon=True,name='spatial-memory');self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=5)

    def accepted_input(self,path,body,result):
        """Nonblocking sanitized command receipt. Control response never waits on disk."""
        if not self.enabled() or not path.startswith('/api/web/'):return
        fields={k:body[k] for k in ('seq','actions','arm','scale','confirmed') if k in body}
        value={'at':stamp(),'op':'input','channel':'web','path':path,'request':fields,
               'result':{k:result[k] for k in ('active','seq','actions','arm','scale','reason') if k in result},
               'accepted':True,'effect_verified':False}
        try:self.pending.put_nowait(value)
        except queue.Full:self.dropped+=1

    def retain_map(self):
        """Preserve every map version observed, including the one present at startup."""
        p=self.survey.root/'map.json'
        if not p.is_file():return
        raw=p.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        if digest==self.last_map:return
        json.loads(raw);history=self.survey.root/'history';history.mkdir(exist_ok=True)
        target=history/('retained-'+digest+'.json')
        if target.exists():
            if target.read_bytes()!=raw:raise ValueError('地图存档哈希冲突')
        else:
            with target.open('xb') as f:f.write(raw)
        self.last_map=digest
        reviews=self.survey.root/'reviews.json'
        if reviews.is_file():
            b=reviews.read_bytes();folder=self.root/'reviews';folder.mkdir(exist_ok=True)
            q=folder/(hashlib.sha256(b).hexdigest()+'.json')
            if not q.exists():q.write_bytes(b)

    def _new_run(self):
        identifier=uuid.uuid4().hex;folder=self.root/identifier;folder.mkdir()
        self.run={'schema':1,'id':identifier,'kind':'spatial_journal','title':'操控与空间连续记录',
                  'started_at':stamp(),'updated_at':stamp(),'finished_at':None,'state':'recording',
                  'events':[],'photos':[],'samples':0,'input_events':0,'depth_frames':0,'gaps':0,
                  'motion_enabled':False,'pose_registered':False,'retention':'no_automatic_deletion'}
        self.folder=folder;self.previous=None;self.last_images=0.;self._flush()

    def _append(self,name,value):
        with (self.folder/name).open('a',encoding='utf8') as f:f.write(json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n')

    def _flush(self,finished=False):
        if not self.run:return
        self.run['updated_at']=stamp();self.run['dropped_input_events']=self.dropped
        if finished:self.run.update(state='closed',finished_at=stamp())
        atomic_json(self.folder/'run.json',self.run);self.last_flush=self.clock()

    def sample(self,state):
        now=self.clock()
        if not self.run:self._new_run()
        motors={f"{p['port']}:{m['id']}":{k:m.get(k) for k in ('position','goal','speed','goal_velocity','torque','voltage','temperature','status')}
                for p in state.get('ports',[]) for m in p['motors']}
        web=state.get('web_control')or{};controller=state.get('controller')or{}
        actor='manual_web' if web.get('active') else 'manual_gamepad' if controller.get('controller')=='online' else 'assistant' if (state.get('agent_run')or{}).get('running') else 'observed_no_active_driver'
        row={'at':stamp(),'t':now,'controller_pid':state.get('server_pid'),'controller_sample_at':state.get('updated_at'),
             'actor':actor,'web':{k:web.get(k) for k in ('active','seq','actions','arm','scale','reason')},
             'controller':{k:controller.get(k) for k in ('controller','paused','last_action','error')},
             'gamepad':{k:(state.get('gamepad')or{}).get(k) for k in ('connected','axes','buttons','hat','updated_at')},'motors':motors}
        current={str(i):motors.get('COM4:'+str(i),{}).get('position') for i in (9,10)}
        before=self.previous
        if before:
            first=before['wheels'];elapsed=now-before['t'];delta={i:(current[i]-first[i]+2048)%4096-2048 for i in first} if all(type(v)is int for v in list(current.values())+list(first.values())) else None
            valid=delta and 0<elapsed<=1.5 and max(abs(v) for v in delta.values())<1000 and before['controller_pid']==state.get('server_pid')
            if valid and any(abs(v)>2 for v in delta.values()):
                manual=actor in ('manual_web','manual_gamepad') or before.get('actor') in ('manual_web','manual_gamepad')
                e={'op':('rotate' if delta['9']*delta['10']>0 else 'travel') if manual else 'observed_motion','at':now,'duration_s':elapsed,
                   'wheel_start_raw':first,'wheel_delta_raw':delta,'stop_readback':current,'command_sign':self.forward_sign,
                   'direction_source':'configured_camera_verified' if self.forward_sign else 'unknown',
                   'status':'done','label':actor+' · 实测双轮变化','message':'输入与编码器结果分别保存；轮估未配准房间，非碰撞或净空证明。','actor':actor}
                self.run['events'].append(e)
            elif not valid:
                self.run['gaps']+=1;self.run['events'].append({'op':'observation_gap','at':now,'status':'failed','label':'采样中断，轨迹重新起点','message':'未跨缺失区间猜测位移'})
                # A new run prevents the playback integrator bridging short gaps.
                self._append('samples.jsonl',row);self._flush(True);self._new_run()
        self._append('samples.jsonl',row);self.run['samples']+=1;self.sample_count+=1
        self.previous={'at':row['at'],'t':now,'wheels':current,'controller_pid':state.get('server_pid'),'actor':actor}
        while True:
            try:command=self.pending.get_nowait()
            except queue.Empty:break
            self._append('inputs.jsonl',command);self.run['input_events']+=1
            self.run['events'].append({'op':'manual_input','at':command['at'],'label':'网页输入 '+','.join(command['request'].get('actions',[])),
                                       'status':'done','message':'控制器已接受；执行结果见相邻遥测与影像。'})
        if now-self.last_flush>4:self._flush()
        return actor

    def _images(self,actor):
        now=self.clock();period=2. if actor in ('manual_web','manual_gamepad','assistant') else 12.
        if now-self.last_images<period:return
        self.last_images=now
        for camera in (0,1,2):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:8765/api/camera/{camera}/snapshot',timeout=.5) as r:
                    b=r.read(3000000);at=float(r.headers['X-Captured-At']);sequence=r.headers['X-Frame-Sequence']
                if not 0<=self.clock()-at<.8:continue
                if self.last_frame.get(camera)==sequence:continue
                name=f'{self.run["samples"]:05d}-cam{camera}.jpg';(self.folder/name).write_bytes(b);self.last_frame[camera]=sequence
                self.run['photos'].append({'camera':str(camera),'file':name,'captured_at':at,'phase':actor,'sha256':hashlib.sha256(b).hexdigest()});self.images+=1
            except (OSError,ValueError,KeyError):continue
        try:
            service,evidence,reconstruction=self.phone_provider()
            frame=service.frame('android');token=(frame.session,frame.sequence)
            if self.last_frame.get('phone')==token:return
            self.last_frame['phone']=token
            saved=evidence.capture('android',frame,'自动空间观察 · '+actor)
            name=f'{self.run["samples"]:05d}-phone.jpg';(self.folder/name).write_bytes(frame.rgb)
            self.run['photos'].append({'camera':'phone','file':name,'captured_at':now,'phase':actor,'time_basis':'host_query_time','capture_id':saved['id']})
            self.images+=1
            if saved.get('meta',{}).get('rgbd'):
                self.depth_captures+=1;self.run['depth_frames']+=1;reconstruction.add(saved,frame)
        except (OSError,ValueError,LookupError,KeyError):pass
        self._flush()

    def _loop(self):
        try:
            while not self.stop_event.is_set():
                began=self.clock()
                try:
                    if self.enabled():
                        self.retain_map()
                        s=self.survey.controller('/api/status');actor=self.sample(s);self._images(actor)
                        if self.clock()-self.last_event_poll>3:
                            self.last_event_poll=self.clock();events=self.survey.controller('/api/events?after='+str(self.last_event or 0))
                            if self.event_pid==s.get('server_pid'):
                                for event in events.get('events',[]):self._append('controller-events.jsonl',event)
                            else:self._append('prior-controller-events.jsonl',{'at':stamp(),'events':events.get('events',[]),'historical_buffer_not_live_input':True})
                            self.last_event=events.get('last_seq');self.event_pid=s.get('server_pid')
                        if self.run['samples']>=1800 or len(self.run['events'])>=4500:self._flush(True);self.run=None;self.previous=None
                    elif self.run:self._flush(True);self.run=None;self.previous=None
                    self.error=None
                except Exception as exc:self.error=type(exc).__name__+': '+str(exc)[:160]
                self.stop_event.wait(max(.05,.5-(self.clock()-began)))
        finally:
            self._flush(True)
