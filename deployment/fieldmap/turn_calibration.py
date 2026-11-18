"""Bounded, locally selected station experiments, using the original executor.

This is an angular calibration experiment in an inspected station. It never
translates, supplies geometric clearance, or writes physical wheel parameters.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
import hashlib,json,math,time,subprocess,os
from pathlib import Path

from .autonomy import AutonomousSurvey,anchor,changed_anchor,replace_json
from .visual_turn import rgb_turn,fit_turn_gain

PLAN=((4,1),(4,-1),(8,1),(8,-1),(12,1),(12,-1),(6,1),(6,-1))


class TurnCalibration(AutonomousSurvey):
    def __init__(self,root,exploration,scan,inputs,phone,*,clock=time.monotonic,tick_s=.35,settle_s=.5,analyser=rgb_turn,rgbd_checker=None):
        super().__init__(root,exploration,scan,inputs,clock=clock,tick_s=tick_s)
        self.phone=phone;self.settle_s=settle_s;self.analyser=analyser;self.keep_task=None
        self.rgbd_checker=rgbd_checker
        self.trial=None;self.images={};self.last_frame=None;self.group_first=None;self.group_last=None
        self.visible_samples=0;self.visible_sequence=None

    def status(self):
        value=super().status()
        value.update(execution='vehicle_local_turn_calibration',bounded_station_experiment=True,
                     automatic_navigation=False,geometry_parameters_written=False,
                     planned_trials=len(PLAN),completed_trials=len(self.state.get('trials',[])),
                     model=deepcopy(self.state.get('model')),closure=deepcopy(self.state.get('closure')))
        if self.state.get('trials'):value['latest_child_id']=self.state['trials'][-1]['child_id']
        if self.trial:value['current_trial']={k:self.trial.get(k) for k in ('index','role','direction','pulses','child_id')}
        return value

    async def start(self,value):
        value=dict(value);review_id=value.pop('review_id',None)
        if not isinstance(review_id,str) or len(review_id)!=32 or any(c not in '0123456789abcdef' for c in review_id):
            raise ValueError('需要当前驻点的停车观察依据')
        review=json.loads((self.scan.root/review_id/'review.json').read_text('utf8'))
        if not 0<=time.time()-review['at']<=90:raise ValueError('驻点观察依据已过期')
        previous=anchor(review['state'])
        # The base class owns the reservation, generation checks and native stop.
        await super().start(value)
        if changed_anchor(previous,self.base_anchor):
            await self.stop('观察后姿态已改变');raise ValueError('驻点参考已改变，不能执行旧试验')
        self.trial=None;self.images={};self.last_frame=None;self.group_first=None;self.group_last=None
        self.visible_samples=0;self.visible_sequence=None
        self.state.update(review_id=review_id,trials=[],model=None,closure=None,
                          progress={'turn_trials':0,'native_pulses':0,'new_area_m2':None},
                          experiment='paired_4_8_12_pulses_then_independent_6_pulse_checks',
                          reference=None,source_wait_checks=0)
        self._save();return self.status()

    def _sample(self):
        import cv2,numpy as np
        service=self.phone()
        if service is None:raise LookupError('iPhone采集尚未接入')
        frame=service.frame('iphone')
        if not frame.raw or not frame.meta.get('rgbd'):raise LookupError('等待iPhone真实RGB-D新帧')
        signature=dict(session=frame.session,width=frame.meta['width'],height=frame.meta['height'],
                       intrinsics=deepcopy(frame.meta['intrinsics']),depth_source=frame.meta.get('depth_source'))
        if self.state['reference'] is None:
            self.state['reference']={**signature,'controller_pid':self.base_anchor['controller_pid']}
        elif any(self.state['reference'][k]!=v for k,v in signature.items()):
            raise RuntimeError('phone_reference_changed')
        if self.last_frame==(frame.session,frame.sequence):return None
        self.last_frame=(frame.session,frame.sequence)
        if self.trial is None:return frame
        folder=self.root/self.state['id']/'trials'/f"{self.trial['index']:03d}"/'input'
        folder.mkdir(parents=True,exist_ok=True);index=len(self.trial['frames'])
        stem=f'{index:04d}';rgb_name=stem+'.jpg';depth_name=stem+'.f32'
        if index>=90:raise RuntimeError('trial_frame_budget_exhausted')
        (folder/rgb_name).write_bytes(frame.rgb);(folder/depth_name).write_bytes(frame.raw)
        row=dict(index=index,session=frame.session,sequence=frame.sequence,meta=deepcopy(frame.meta),
                 at=time.time(),received=frame.received,source_age_ms=max(0,(service.clock()-frame.received)*1000),
                 rgb_file=rgb_name,depth_file=depth_name,rgb_sha256=hashlib.sha256(frame.rgb).hexdigest(),
                 depth_sha256=hashlib.sha256(frame.raw).hexdigest())
        self.trial['frames'].append(row)
        replace_json(folder/'frames.json',dict(frames=self.trial['frames'],collector_motion_commands=0,
                     motion_child_id=self.trial.get('child_id')))
        image=cv2.imdecode(np.frombuffer(frame.rgb,np.uint8),cv2.IMREAD_GRAYSCALE)
        if image is None or list(image.shape[::-1])!=[signature['width'],signature['height']]:
            raise RuntimeError('phone_image_dimensions_changed')
        self.images[frame.sequence]=image
        if self.group_first is None:self.group_first=image.copy()
        if 'first_sequence' not in self.trial:self.trial['first_sequence']=frame.sequence
        self.trial['last_sequence']=frame.sequence
        return frame

    def _static_reference_changed(self,current):
        old=deepcopy(self.base_anchor);new=deepcopy(current)
        for key in ('COM4:9','COM4:10'):
            old['positions'].pop(key,None);new['positions'].pop(key,None)
        return changed_anchor(old,new)

    def _visibility_issues(self):
        """Reject almost-black required views; this does not infer clearance."""
        import cv2,numpy as np
        issues=[];sequences=[]
        for camera in (0,1,2):
            raw,meta=self.scan.frames.get(camera,(b'',{}))
            gray=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_GRAYSCALE) if raw else None
            if gray is None or float(gray.mean())<12 or float((gray<20).mean())>.92:
                issues.append('required_view_too_dark:'+str(camera))
            sequences.append(meta.get('sequence'))
        if issues:self.visible_samples=0;self.visible_sequence=None
        elif tuple(sequences)!=self.visible_sequence:
            self.visible_samples+=1;self.visible_sequence=tuple(sequences)
        return issues

    async def _tick(self):
        try:observation=await self.inputs()
        except Exception as exc:
            if self.scan.task and not self.scan.task.done():
                self._latch('feedback_unknown_during_action');await self.scan.stop('校对反馈未知');return
            self._transition('waiting_source',['local_input_unavailable:'+str(exc)[:160]]);return
        self.state['latest_inputs']={k:deepcopy(observation[k]) for k in ('phone','ownership','perception') if k in observation}
        reason=observation.get('hard_stop') or self._static_reference_changed(observation['anchor'])
        if reason:
            self._latch(reason);await self.scan.stop('校对参考或操作员已改变');return
        # The native runner persists final parking before its task wrapper gets
        # the next event-loop turn. Do not misclassify that parked result as an
        # in-flight phone fault and hide a partial physical receipt.
        child_active=bool(self.scan.task and not self.scan.task.done() and self.scan.state.get('running'))
        if not self.state.get('child_id') and changed_anchor(self.base_anchor,observation['anchor']):
            self._latch('uncommanded_pose_change');return
        if self.state.get('child_id'):
            child=self.scan.status()
            if child.get('id')!=self.state['child_id']:
                self._latch('child_identity_changed');return
            if not child_active and self.trial.get('child_finished') and changed_anchor(self.base_anchor,observation['anchor']):
                self._latch('uncommanded_pose_change_after_trial');return
            if not child_active and not self.trial.get('child_finished'):
                result=self.scan.read(child['id'])
                rotations=[r for r in result.get('native_receipt',{}).get('results',[]) if r.get('op')=='rotate']
                self.trial.update(child_result=result,child_finished=True,finished_clock=self.clock(),rotations=rotations)
                if (child.get('phase')!='completed' or child.get('review_required') or not child.get('parked_verified') or
                    len(rotations)!=self.trial['pulses'] or any(r.get('status')!='done' for r in rotations)):
                    self._latch('physical_trial_failed_or_uncertain');return
                self.base_anchor=anchor(result['final_state'])
                self.state['parked_verified']=True
                self.trial['settle_frames']=0;self._save();return
        try:frame=self._sample()
        except LookupError:
            if child_active:
                self._latch('phone_source_lost_during_trial');await self.scan.stop('校对手机源丢失');return
            self._transition('waiting_source',['iphone_rgbd_required']);self.state['source_wait_checks']+=1;return
        except RuntimeError as exc:
            self._latch(str(exc));await self.scan.stop('校对源参考变化');return
        if self.state.get('child_id'):
            if child_active:
                issues=self._visibility_issues()
                if issues:
                    self._latch('visibility_lost_during_trial');await self.scan.stop('校对关键视图变暗');return
                self.state.update(phase='calibrating',note='本地执行有限转动并采集，等待原执行器回执')
                self._save();return
            if frame is not None:self.trial['settle_frames']+=1
            if self.clock()-self.trial['finished_clock']<self.settle_s or self.trial['settle_frames']<3:return
            await self._finish_trial();return
        if not observation['hardware_ready'] or not observation['sources_fresh']:
            self._transition('waiting_source' if not observation['sources_fresh'] else 'waiting_hardware',
                             observation['source_issues'] if not observation['sources_fresh'] else observation['hardware_issues']);return
        if len(self.state['trials'])==len(PLAN):
            self.state['phase']='checking_model';self._save()
            model=fit_turn_gain(self.state['trials'][:6],self.state['trials'][6:])
            closure=await asyncio.to_thread(self.analyser,self.group_first,self.group_last,self.state['reference']['intrinsics'])
            closure['heading_revisit_passed']=bool(closure.get('accepted') and closure.get('rotation_magnitude_deg',math.inf)<=3)
            self.state['closure']=closure;model['gain_validation_passed']=model['accepted']
            if any(not r.get('rgbd',{}).get('check_passed') for r in self.state['trials'][6:]):
                model.update(accepted=False,reason='held_out_rgbd_cross_check_failed')
            if not closure['heading_revisit_passed']:
                model.update(accepted=False,reason='initial_heading_revisit_check_failed')
            self.state.update(model=model,phase='completed' if model['accepted'] else 'not_adopted',completed_task=True,
                              note='独立正反向检查通过；修正仅用于本场转角估计' if model['accepted'] else '证据未达到采纳条件；原始结果完整保留')
            self._event('calibration_checked',accepted=model['accepted'],reason=model['reason']);return
        issues=self._visibility_issues()
        if issues or self.visible_samples<3:
            self._transition('waiting_visibility',issues or ['three_fresh_visible_sets_required']);return
        index=len(self.state['trials']);pulses,direction=PLAN[index]
        # The complete bounded experiment was inspected once. Each new child
        # still obtains current parking and camera evidence locally.
        if self.clock()<self.preflight_after:return
        generation=self.generation
        try:review=await self.scan.review(owner=self.state['id'])
        except ValueError as exc:
            # This call has no actuator submission. Unknown start outcomes are
            # deliberately outside this retry scope.
            self.preflight_failures+=1;self.preflight_after=self.clock()+min(10,2*self.preflight_failures)
            self._event('preflight_deferred',reason=str(exc),rejections=self.preflight_failures,motion_attempts=0)
            if self.preflight_failures>=10:self._latch('local_preflight_recovery_exhausted')
            return
        if self.cancelled or generation!=self.generation or not self._mission() or self._budget_reached():return
        issues=self._visibility_issues()
        if issues:self._transition('waiting_visibility',issues);return
        self.trial=dict(index=index,id=f"{self.state['id']}-{index}",role='fit' if index<6 else 'validation',
                        pulses=pulses,direction=direction,frames=[])
        self.images={};self.last_frame=None
        try:self._sample()
        except LookupError:
            self.trial=None;self._transition('waiting_source',['iphone_rgbd_required']);return
        child=await self.scan.start(dict(confirmed=True,authorization=self.state['authorization'],profile='turn_batch',
            mission_id=self.state['mission_id'],review_id=review['review_id'],pulses=pulses,direction=direction),owner=self.state['id'])
        self.trial['child_id']=child['id'];self.state['child_id']=child['id'];self.state['current_trial']=self.trial
        self.state['parked_verified']=False
        self._event('local_calibration_trial_selected',index=index,pulses=pulses,direction=direction,role=self.trial['role'],model_approval=False)

    async def _finish_trial(self):
        row=self.trial;first=row['frames'][0];last=row['frames'][-1]
        visual=await asyncio.to_thread(self.analyser,self.images[row['first_sequence']],self.images[row['last_sequence']],first['meta']['intrinsics'])
        self.group_last=self.images[row['last_sequence']].copy()
        design=row['direction']*sum(abs(r['yaw_delta_deg_estimate']) for r in row['rotations'])
        summary=dict(id=row['id'],role=row['role'],reference=deepcopy(self.state['reference']),
                     native_completed=True,parked_verified=True,design_angle_deg=design,visual=visual,
                     child_id=row['child_id'],native_run_id=row['child_result'].get('native_run_id'),
                     pulses=row['pulses'],frames=len(row['frames']),first_sequence=first['sequence'],last_sequence=last['sequence'])
        if row['role']=='validation':
            self.state.update(phase='checking_rgbd',note='停车后用独立验证组核对RGB-D转角');self._save()
            summary['rgbd']=await (self.rgbd_checker(row) if self.rgbd_checker else self._rgbd_check(row))
            if self.cancelled:return
            if summary['rgbd'].get('ok'):
                difference=abs(summary['rgbd']['rotation_magnitude_deg']-visual.get('rotation_magnitude_deg',math.inf))
                summary['rgbd']['difference_from_rgb_deg']=difference
                summary['rgbd']['check_passed']=bool(difference<=max(3.,visual.get('rotation_magnitude_deg',0)*.25))
        folder=self.root/self.state['id']/'trials'/f"{row['index']:03d}"
        replace_json(folder/'result.json',summary)
        self.state['trials'].append(summary);self.state['progress']['turn_trials']+=1
        self.state['progress']['native_pulses']+=len(row['rotations'])
        # A partial/failed experiment never drops already completed motor trials.
        m=self._mission()
        if m:self.exploration.update(m['session_id'],'mapping','车载转角校对已完成'+str(len(self.state['trials']))+'组',
            translation_legs=m['translation_legs'],turn_attempts=m['turn_attempts']+len(row['rotations']),wheel_path_m=m['wheel_path_m'])
        if not visual.get('accepted'):
            self.state.update(phase='not_adopted',completed_task=True,note='图像不足以校对；停车保留实际动作与证据')
        self.last_progress=self.clock();self.no_progress_reported=False
        self.state.pop('child_id',None);self.state.pop('current_trial',None);self.trial=None
        self._event('calibration_trial_completed',trial=summary)

    async def _rgbd_check(self,row):
        folder=self.root/self.state['id']/'trials'/f"{row['index']:03d}"
        service=self.phone();repo=Path(service.repo);worker=repo/'.venv-dense/Scripts/python.exe'
        if not worker.is_file():return dict(ok=False,reason='existing_rgbd_backend_unavailable')
        env=dict(os.environ,OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1')
        with (folder/'rgbd-worker.log').open('wb') as log:
            process=subprocess.Popen([str(worker),'-m','deployment.phone_vision.dense','--input',str(folder/'input'),
                '--output',str(folder/'rgbd'),'--pose-mode','odometry'],cwd=str(repo),env=env,stdout=log,stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            deadline=time.monotonic()+120
            try:
                while process.poll() is None:
                    if self.cancelled or not self._mission() or self._budget_reached() or time.monotonic()>deadline:
                        return dict(ok=False,reason='stopped' if self.cancelled else 'rgbd_budget_reached')
                    await asyncio.sleep(.2)
                if process.returncode:return dict(ok=False,reason='rgbd_backend_rejected',exit_code=process.returncode)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=2)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
        report=json.loads((folder/'rgbd/report.json').read_text('utf8'))
        import numpy as np
        poses=[np.array(r['camera_to_local']) for r in report['accepted']]
        if len(poses)<3 or len(poses)<.7*report['input_frames']:
            return dict(ok=False,reason='insufficient_rgbd_alignment',accepted=len(poses),input=report['input_frames'])
        relative=np.linalg.inv(poses[0])@poses[-1]
        angle=math.degrees(math.acos(np.clip((np.trace(relative[:3,:3])-1)/2,-1,1)))
        return dict(ok=True,rotation_magnitude_deg=angle,accepted=len(poses),input=report['input_frames'],
                    alignment_rmse_median_m=report['alignment_rmse_median_m'],metric_accuracy_verified=False,
                    registered_robot_map=False)

    def _budget_reached(self):
        return self.clock()-self.begun>=self.state['policy']['max_duration_s']

    def _heartbeat(self):
        if self.clock()<self.heartbeat_after and self.heartbeat_phase==self.state['phase']:return
        m=self._mission()
        if m:
            self.exploration.update(m['session_id'],'moving' if self.state.get('child_id') else 'guarded_stop',
                '本地转角校对：'+self.state['phase']+'；'+str(len(self.state.get('trials',[])))+'/8组',
                **{k:m[k] for k in ('translation_legs','turn_attempts','wheel_path_m')})
        self.heartbeat_after=self.clock()+15;self.heartbeat_phase=self.state['phase']

    async def _keep_views(self):
        while not self.cancelled:
            # The child watchdog already reads all three cameras every180ms.
            # This lease is needed across parked analysis and review gaps.
            if not (self.scan.task and not self.scan.task.done()):
                try:await self.scan._views()
                except Exception:pass  # The input/native watchdogs still reject missing sources.
            await asyncio.sleep(.25)

    async def _run(self):
        self.keep_task=asyncio.create_task(self._keep_views())
        try:await super()._run()
        finally:
            self.keep_task.cancel();await asyncio.gather(self.keep_task,return_exceptions=True)
