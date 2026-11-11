"""Append observations to separate, persistent AR capture-session models."""
from __future__ import annotations
import hashlib,json,math,threading
from pathlib import Path
from io import BytesIO
from PIL import Image
from .evidence import geometry
from deployment.fieldmap.store import atomic_json


class Reconstruction:
    def __init__(self,evidence):self.evidence=evidence;self.lock=threading.RLock()

    def add(self,saved,frame):
        capture=frame.meta.get('arcore_capture_session')
        if not capture or not frame.meta.get('pose_valid') or frame.meta.get('tracking')!='TRACKING':return None
        cloud=geometry(frame,world=True,max_points=4000)
        with self.lock:
            session_key=hashlib.sha256(str(capture).encode()).hexdigest()
            index=self.evidence.root/'reconstruction'/session_key;index.mkdir(parents=True,exist_ok=True)
            state_path=index/'state.json';part=json.loads(state_path.read_text('utf8'))['part'] if state_path.exists() else 0
            identifier=hashlib.sha256(('persistent-arcore:'+str(capture)+':'+str(part)).encode()).hexdigest()[:32]
            prior=self.evidence.folder('models',identifier)/'meta.json'
            if prior.exists() and json.loads(prior.read_text('utf8'))['source_frames']>=500:
                part+=1;identifier=hashlib.sha256(('persistent-arcore:'+str(capture)+':'+str(part)).encode()).hexdigest()[:32]
            atomic_json(state_path,{'capture_session':capture,'part':part,'no_old_parts_deleted':True})
            folder=self.evidence.folder('models',identifier);folder.mkdir(parents=True,exist_ok=True)
            path=folder/'meta.json';meta=json.loads(path.read_text('utf8')) if path.exists() else {
                'id':identifier,'label':'AR空间累积 · '+str(capture)[-8:]+' · '+str(part+1),'saved_at':saved['saved_at'],'point_count':0,
                'capture_session':capture,'source_captures':[],'source_frames':0,'historical':True,'calibrated':False,
                'registered_robot_map':False,'motion_enabled':False,'retention':'source_frames_preserved_no_automatic_deletion'}
            if saved['id'] in meta['source_captures']:return meta
            model=json.loads((folder/'cloud.json').read_text('utf8')) if (folder/'cloud.json').exists() else {
                'positions':[],'colors':[],'trajectory':[],'coordinates':'arcore_world_opengl','capture_session':capture,
                'calibrated':False,'registered_robot_map':False,'voxel_m':.04,'source_frames':0}
            voxels={tuple(round(v/.04) for v in model['positions'][i:i+3]):(model['positions'][i:i+3],model['colors'][i:i+3]) for i in range(0,len(model['positions']),3)}
            with Image.open(BytesIO(frame.rgb)) as im:
                rgb=im.convert('RGB')
                if 'rgb_pixels' not in cloud:rgb=rgb.resize((cloud['width'],cloud['height']))
                elif rgb.size!=(cloud['rgb_width'],cloud['rgb_height']):raise ValueError('彩色成像尺寸与像素映射不一致')
            color_pixels=cloud.get('rgb_pixels',cloud['pixels'])
            for n in range(cloud['count']):
                point=cloud['positions'][n*3:n*3+3];key=tuple(round(v/.04) for v in point)
                if key not in voxels and len(voxels)<60000:voxels[key]=(point,list(rgb.getpixel(tuple(color_pixels[n*2:n*2+2]))))
            model['positions']=[v for p,c in voxels.values() for v in p];model['colors']=[v for p,c in voxels.values() for v in c]
            model['count']=len(voxels);model['source_frames']+=1
            pose=cloud.get('pose_translation')
            if pose and (not model['trajectory'] or sum((a-b)**2 for a,b in zip(pose,model['trajectory'][-1]))>.0001):model['trajectory'].append(pose)
            meta['source_captures'].append(saved['id']);meta.update(source_frames=model['source_frames'],point_count=model['count'],updated_at=saved['saved_at'],
                last_depth_quality=cloud['depth_quality'],raw_high_confidence_fraction=cloud['raw_high_confidence_fraction'],
                display_point_budget=60000,all_source_frames_retained=True)
            atomic_json(folder/'cloud.json',model);atomic_json(path,meta)
            if not (folder/'preview.jpg').exists():(folder/'preview.jpg').write_bytes(frame.rgb)
            return meta
