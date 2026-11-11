"""Phone image evidence and bounded depth geometry; never controls a robot."""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import threading
import uuid

IDENTIFIER = re.compile(r'^[a-f0-9]{32}$')


def depth_quality(raw):
    """Describe supplied estimates; positive numbers alone do not establish useful depth."""
    count=len(raw)//4;positive=finite=in_range=0;sample=[];minimum=math.inf;maximum=-math.inf
    stride=max(1,math.ceil(count/8192))
    for i,(value,) in enumerate(struct.iter_unpack('<f',raw)):
        if not math.isfinite(value):continue
        finite+=1
        if value<=0:continue
        positive+=1;minimum=min(minimum,value);maximum=max(maximum,value)
        if .2<=value<=5:in_range+=1
        if i%stride==0:sample.append(value)
    sample.sort()
    return {'pixels':count,'finite_fraction':finite/count if count else 0.,
            'positive_fraction':positive/count if count else 0.,
            'analysis_window_m':[.2,5.],'analysis_range_fraction':in_range/count if count else 0.,
            'analysis_range_pixels':in_range,'positive_min_m':minimum if positive else None,
            'positive_max_m':maximum if positive else None,
            'sampled_positive_median_m':sample[len(sample)//2] if sample else None,
            'metric_accuracy_verified':False,'navigation_validated':False}


def geometry(frame, *, world=False, max_points=6000):
    m = frame.meta
    if not m.get('rgbd') or not frame.raw:
        raise ValueError('当前画面没有有效深度；可查看彩色或已保存的空间结果')
    w, h = m['width'], m['height']
    if type(w) is not int or type(h) is not int or not 0 < w <= 4096 or not 0 < h <= 4096 or len(frame.raw) != w*h*4:
        raise ValueError('深度尺寸或数组长度不一致')
    k = m.get('intrinsics') or {}
    fx, fy, cx, cy = (float(k[n]) for n in ('fx', 'fy', 'tx', 'ty'))
    if not all(math.isfinite(v) for v in (fx, fy, cx, cy)) or min(fx, fy) <= 0:
        raise ValueError('相机内参无效')
    if type(max_points) is not int or not 100 <= max_points <= 12000:
        raise ValueError('点数超出展示预算')
    matrix = translation = None
    if world:
        if not (m.get('android_mode') == 'arcore' and m.get('pose_valid') and m.get('tracking') == 'TRACKING' and m.get('arcore_capture_session')):
            raise ValueError('当前帧没有同会话AR位姿，不能转换到空间坐标')
        q = m.get('pose_quaternion') or []
        translation = m.get('pose_translation') or []
        if len(q) != 4 or len(translation) != 3 or not all(math.isfinite(v) for v in q+translation) or abs(sum(v*v for v in q)-1) > .01:
            raise ValueError('AR位姿无效')
        x,y,z,a = q
        matrix = ((1-2*(y*y+z*z),2*(x*y-z*a),2*(x*z+y*a)),
                  (2*(x*y+z*a),1-2*(x*x+z*z),2*(y*z-x*a)),
                  (2*(x*z-y*a),2*(y*z+x*a),1-2*(x*x+y*y)))
    rotation = m.get('rotation_deg', 0)
    if rotation not in (0,90,180,270):
        raise ValueError('不支持的相机旋转')
    stride = max(1, math.ceil(math.sqrt(w*h/max_points)))
    positions, pixels = [], []
    for py in range(0,h,stride):
        for px in range(0,w,stride):
            z = struct.unpack_from('<f', frame.raw, (py*w+px)*4)[0]
            if not math.isfinite(z) or not .2 <= z <= 5:
                continue
            x,y = (px-cx)*z/fx, (py-cy)*z/fy
            if matrix is not None:
                x,y = {0:(x,y),90:(y,-x),180:(-x,-y),270:(-y,x)}[rotation]
                vector=(x,-y,-z)
                point=[sum(a*b for a,b in zip(row,vector))+t for row,t in zip(matrix,translation)]
            else:
                point=(x,-y,-z)
            positions.extend(round(v,5) for v in point);pixels.extend((px,py))
    return {'positions':positions,'pixels':pixels,'count':len(pixels)//2,'width':w,'height':h,
            'session':frame.session,'sequence':frame.sequence,'depth_source':m.get('depth_source','unknown'),
            'coordinates':'arcore_world_opengl' if world else 'display_camera_opengl',
            'capture_session':m.get('arcore_capture_session') if world else None,
            'pose_translation':translation,'pose_quaternion':m.get('pose_quaternion') if world else None,
            'analysis_window_m':[.2,5.],'calibrated':False,'registered_robot_map':False,
            'depth_quality':depth_quality(frame.raw),
            'confidence_available':bool(m.get('confidence_available')),
            'raw_high_confidence_fraction':m.get('raw_depth_high_confidence_fraction')}


class Evidence:
    def __init__(self, root):
        self.root=Path(root);self.lock=threading.RLock()

    def folder(self, kind, identifier):
        if kind not in ('captures','models') or not IDENTIFIER.fullmatch(identifier):
            raise KeyError('记录不存在')
        path=self.root/kind/identifier
        if not path.resolve().is_relative_to((self.root/kind).resolve()):raise KeyError('记录不在图像目录内')
        return path

    def list(self, kind='captures', limit=48):
        if kind not in ('captures','models') or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('无效的记录查询')
        items=[]
        for p in sorted((self.root/kind).glob('*/meta.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:limit]:
            if not IDENTIFIER.fullmatch(p.parent.name) or p.stat().st_size > 200000:
                continue
            try:items.append(self._presentation(json.loads(p.read_text('utf8'))))
            except (OSError,ValueError):continue
        return {'items':items,'kind':kind,'limit':limit,'motion_enabled':False}

    def read(self,kind,identifier):
        p=self.folder(kind,identifier)/'meta.json'
        if not p.is_file() or p.stat().st_size>200000:raise KeyError('记录不存在')
        return self._presentation(json.loads(p.read_text('utf8')))

    @staticmethod
    def _presentation(value):
        # Correct older zero-point records on read; preserve their original files.
        if value.get('point_count')==0:value['cloud_available']=False
        return value

    def media(self,kind,identifier,name):
        allowed={'rgb.jpg','depth.jpg','depth.f32','preview.jpg','cloud.json'}
        if name not in allowed:raise KeyError('媒体不存在')
        p=self.folder(kind,identifier)/name
        if not p.is_file() or p.stat().st_size>12*1024*1024:raise KeyError('媒体不存在')
        return p

    def capture(self, device, frame, label=''):
        if device not in ('android','iphone') or not isinstance(label,str) or len(label)>80:
            raise ValueError('无效的拍摄说明')
        with self.lock:
            cloud=geometry(frame,world=bool(frame.meta.get('pose_valid') and frame.meta.get('android_mode')=='arcore')) if frame.raw else None
            identifier=uuid.uuid4().hex;folder=self.folder('captures',identifier);folder.mkdir(parents=True)
            files={}
            for name,data in (('rgb.jpg',frame.rgb),('depth.jpg',frame.depth),('depth.f32',frame.raw)):
                if data:
                    (folder/name).write_bytes(data);files[name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
            # Store camera metadata, without executable paths or transport endpoints.
            keys=('width','height','fps','camera_id','rotation_deg','transport','role','android_mode','rgbd','intrinsics','tracking',
                  'depth_source','sensor_raw_depth','confidence_available','raw_depth_high_confidence_fraction','raw_depth_estimate_age_ms',
                  'positive_depth_fraction','depth_02_to_5m_fraction','raw_depth_positive_fraction','raw_depth_confidence_available',
                  'depth_age_ms','device_clock_uncertainty_ms','depth_rgb_registration',
                  'pose_valid','pose_translation','pose_quaternion','pose_coordinates','arcore_capture_session','frame_timestamp_ns','depth_timestamp_ns')
            meta={k:frame.meta[k] for k in keys if k in frame.meta}
            value={'id':identifier,'device':device,'label':label or '手机观察','saved_at':datetime.now().astimezone().isoformat(),
                   'session':frame.session,'sequence':frame.sequence,'meta':meta,'files':files,'historical':True,
                   'calibrated':False,'registered_robot_map':False,'motion_enabled':False}
            if cloud is not None:
                (folder/'cloud.json').write_text(json.dumps(cloud,separators=(',',':')),encoding='utf8')
                value['cloud_available']=cloud['count']>0;value['point_count']=cloud['count'];value['depth_quality']=cloud['depth_quality']
            (folder/'meta.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
            return value
