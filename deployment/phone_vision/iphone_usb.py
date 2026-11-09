"""Bounded Record3D USB probe. Requires the iPhone App to stream RGB-D.

USB enumeration alone is not success. Actual arrays and valid numerical depth
are required. No installation, account access, pairing changes or robot I/O.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import threading
import time


def depth_summary(depth):
    """Describe numerical validity; positive depth is not calibrated accuracy."""
    import numpy as np
    valid=np.isfinite(depth)&(depth>0)
    values=depth[valid]
    result={'valid_positive_depth_fraction':float(valid.mean()) if depth.size else 0.,
            'valid_positive_depth_pixels':int(valid.sum())}
    if values.size:
        result['positive_depth_range_m']=[float(values.min()),float(values.max())]
        result['positive_depth_p05_p50_p95_m']=list(map(float,np.percentile(values,[5,50,95])))
    return result


def probe(output, duration=15):
    import numpy as np
    import record3d

    if not 1 <= duration <= 300:
        raise ValueError('Probe duration must be 1..300 seconds')
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    ready=threading.Event()
    stopped=threading.Event()
    arrivals=[]
    guard=threading.Lock()
    latest={}
    callback_errors=[]
    def on_frame():
        # Copy inside the native callback: the receiver cannot advance its
        # shared arrays to another RGB-D packet until this callback returns.
        try:
            now=time.monotonic()
            rgb=np.asarray(stream.get_rgb_frame()).copy()
            depth=np.asarray(stream.get_depth_frame()).copy()
            confidence=np.asarray(stream.get_confidence_frame()).copy()
            with guard:
                arrivals.append(now)
                latest.update(rgb=rgb,depth=depth,confidence=confidence,sequence=len(arrivals))
            ready.set()
        except Exception as exc:
            with guard:
                callback_errors.append(type(exc).__name__+': '+str(exc))
            stopped.set()
    stream=record3d.Record3DStream()
    stream.on_new_frame=on_frame
    stream.on_stream_stopped=stopped.set
    devices=stream.get_connected_devices()
    result={'transport':'Record3D USB','usb_device_count':len(devices),'frame_received':False,
            'valid_depth_received':False,'robot_commands':0,'requested_duration_s':duration}
    snapshots=0
    started=time.monotonic()
    last_sequence=0
    depth_hashes=set()
    validity=[]
    first_saved=False
    final_arrays=None
    try:
        if len(devices)!=1:
            raise ValueError('Exactly one USB iOS device required')
        result['connected']=bool(stream.connect(devices[0]))
        print(json.dumps({'event':'usb_session','connected':result['connected']},ensure_ascii=False),flush=True)
        if not result['connected']:
            result['reason']='No App RGB-D streaming session accepted; start Record3D USB streaming on the iPhone'
        else:
            deadline=time.monotonic()+duration
            while time.monotonic()<deadline and not stopped.is_set():
                if not ready.wait(min(1,max(0,deadline-time.monotonic()))):
                    continue
                ready.clear()
                with guard:
                    current=latest.copy()
                if not current or current['sequence']==last_sequence:
                    continue
                last_sequence=current['sequence']
                rgb,depth,confidence=current['rgb'],current['depth'],current['confidence']
                if not rgb.size or not depth.size:
                    continue
                stats=depth_summary(depth)
                result.update(frame_received=True,rgb_shape=list(rgb.shape),depth_shape=list(depth.shape),
                              depth_dtype=str(depth.dtype),depth_unit='m (Record3D API)',
                              confidence_shape=list(confidence.shape),**stats)
                if stats['valid_positive_depth_pixels']:
                    result['valid_depth_received']=True
                validity.append(stats['valid_positive_depth_fraction'])
                depth_hashes.add(hashlib.sha256(depth.tobytes()).hexdigest())
                intrinsics=stream.get_intrinsic_mat()
                result['rgb_intrinsics']={k:float(getattr(intrinsics,k)) for k in ('fx','fy','tx','ty')}
                result['camera_type']=int(stream.get_device_type())
                final_arrays={'rgb':rgb,'depth':depth,'confidence':confidence,
                              'intrinsics':np.array([[intrinsics.fx,0,intrinsics.tx],[0,intrinsics.fy,intrinsics.ty],[0,0,1]])}
                if not first_saved:
                    np.savez_compressed(output/'first-rgbd-private.npz',**final_arrays)
                    result['first_frame_after_launch_s']=round(time.monotonic()-started,3)
                    print(json.dumps({'event':'first_rgbd','rgb_shape':list(rgb.shape),'depth_shape':list(depth.shape),
                                      'camera_type':result['camera_type'],**stats},ensure_ascii=False),flush=True)
                    first_saved=True
                snapshots+=1
            if not result['frame_received']:
                result['reason']='Session connected but no RGB-D arrays arrived within the bounded probe'
    except Exception as exc:
        result['error']=type(exc).__name__+': '+str(exc)
    finally:
        result['stopped_before_disconnect']=stopped.is_set()
        try:
            stream.disconnect()
        except Exception:
            pass
        with guard:
            result['frame_callbacks']=len(arrivals)
            if callback_errors:
                result['callback_errors']=callback_errors[:3]
            if len(arrivals)>1:
                span=arrivals[-1]-arrivals[0]
                result['callback_span_s']=round(span,3)
                result['callback_fps']=round((len(arrivals)-1)/span,3) if span else None
                result['callback_gap_p95_ms']=round(float(np.percentile(np.diff(arrivals)*1000,95)),3)
                result['callback_gap_max_ms']=round(float(np.diff(arrivals).max()*1000),3)
        if final_arrays:
            np.savez_compressed(output/'latest-rgbd-private.npz',**final_arrays)
        result['distinct_depth_array_hashes']=len(depth_hashes)
        if validity:
            result['positive_depth_fraction_min_p50_max']=[float(min(validity)),float(np.median(validity)),float(max(validity))]
        result['snapshots_read']=snapshots
        result['elapsed_s']=round(time.monotonic()-started,3)
        result['success']=bool(result['frame_received'] and result['valid_depth_received'] and not result.get('error') and not callback_errors)
        (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    return result


def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--duration',type=int,default=15)
    args=parser.parse_args()
    result=probe(**vars(args))
    raise SystemExit(0 if result['success'] else 1)


if __name__=='__main__':
    main()
