"""Durable supervision state and stop latch. Does not generate motor commands."""
from __future__ import annotations
from datetime import datetime
import json
from pathlib import Path
import threading
import time
import uuid
from .store import atomic_json


class Exploration:
    def __init__(self, root, *, clock=time.time):
        self.root=Path(root);self.path=self.root/'state.json';self.clock=clock;self.lock=threading.RLock()

    def _read(self):
        if not self.path.exists():return {'enabled':False,'phase':'idle','session_id':None,'note':'尚未开始持续探索','revision':0}
        return json.loads(self.path.read_text('utf8'))

    def status(self):
        with self.lock:
            data=self._read();age=max(0,self.clock()-data.get('heartbeat_at',0)) if data.get('heartbeat_at') else None
            return {**data,'heartbeat_age_s':age,'supervisor_connected':bool(data['enabled'] and age is not None and age<90),
                    'motion_permission':False,'execution':'assistant_supervised_finite_segments','automatic_geofence':False}

    def start(self, authorization, confirmed):
        if confirmed is not True or not isinstance(authorization,str) or not 8<=len(authorization)<=2000:
            raise ValueError('需要持续探索的明确任务授权')
        with self.lock:
            old=self._read()
            if old['enabled']:return self.status()
            self.root.mkdir(parents=True,exist_ok=True)
            if self.path.exists():(self.root/('previous-'+uuid.uuid4().hex+'.json')).write_bytes(self.path.read_bytes())
            data={'enabled':True,'phase':'observing','session_id':uuid.uuid4().hex,'authorization':authorization,
                  'started_at':datetime.now().astimezone().isoformat(),'heartbeat_at':self.clock(),'note':'正在观察并选择下一段通路',
                  'revision':old.get('revision',0)+1,'translation_legs':0,'turn_attempts':0,'wheel_path_m':0.,'right_lift_hold_retained':True}
            atomic_json(self.path,data);return self.status()

    def update(self,session_id,phase,note,*,translation_legs=0,turn_attempts=0,wheel_path_m=0.):
        if phase not in ('observing','planning','moving','mapping','updating_ui','guarded_stop') or not isinstance(note,str) or len(note)>2000:
            raise ValueError('无效的探索状态')
        if any(type(v) is not int or v<0 for v in (translation_legs,turn_attempts)) or type(wheel_path_m) not in (float,int) or not 0<=wheel_path_m<100000:
            raise ValueError('无效的探索统计')
        with self.lock:
            data=self._read()
            if not data['enabled'] or data['session_id']!=session_id:raise ValueError('探索已停止或会话已变化，禁止恢复旧动作')
            data.update(phase=phase,note=note,heartbeat_at=self.clock(),revision=data['revision']+1,
                        translation_legs=translation_legs,turn_attempts=turn_attempts,wheel_path_m=wheel_path_m)
            atomic_json(self.path,data);return self.status()

    def stop(self,stop_controller):
        with self.lock:
            self.root.mkdir(parents=True,exist_ok=True);data=self._read()
            data.update(enabled=False,phase='stopped',note='已请求停止持续探索',stopped_at=datetime.now().astimezone().isoformat(),revision=data.get('revision',0)+1)
            atomic_json(self.path,data)
        errors=[]
        for path in ('/api/agent/stop','/api/servo/stop'):
            try:stop_controller(path,{'reason':'用户停止持续探索'})
            except Exception as exc:errors.append(type(exc).__name__+': '+str(exc))
        return {'requested':True,'errors':errors,'state':self.status(),'physical_parking_requires_live_readback':True}
