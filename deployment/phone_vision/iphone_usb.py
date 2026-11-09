"""Bounded Record3D USB probe. Requires the iPhone App to stream RGB-D.

USB enumeration alone is not success. Actual arrays and valid numerical depth
are required. No installation, account access, pairing changes or robot I/O.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import threading
import time


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
    def on_frame():
        with guard:
            arrivals.append(time.monotonic())
        ready.set()
    stream=record3d.Record3DStream()
    stream.on_new_frame=on_frame
    stream.on_stream_stopped=stopped.set
    devices=stream.get_connected_devices()
    result={'transport':'Record3D USB','usb_device_count':len(devices),'frame_received':False,
            'valid_depth_received':False,'robot_commands':0}
    snapshots=0
    try:
        if len(devices)!=1:
            raise ValueError('Exactly one USB iOS device required')
        result['connected']=bool(stream.connect(devices[0]))
        if not result['connected']:
            result['reason']='No App RGB-D streaming session accepted; start Record3D USB streaming on the iPhone'
        else:
            deadline=time.monotonic()+duration
            while time.monotonic()<deadline and not stopped.is_set():
                if not ready.wait(min(1,max(0,deadline-time.monotonic()))):
                    continue
                ready.clear()
                rgb=np.asarray(stream.get_rgb_frame()).copy()
                depth=np.asarray(stream.get_depth_frame()).copy()
                if not rgb.size or not depth.size:
                    continue
                valid=np.isfinite(depth)&(depth>0)
                result.update(frame_received=True,rgb_shape=list(rgb.shape),depth_shape=list(depth.shape),
                              depth_dtype=str(depth.dtype),depth_unit='m (Record3D API)',
                              valid_positive_depth_fraction=float(valid.mean()))
                if valid.any():
                    result.update(valid_depth_received=True,positive_depth_range_m=[float(depth[valid].min()),float(depth[valid].max())])
                intrinsics=stream.get_intrinsic_mat()
                result['rgb_intrinsics']={k:float(getattr(intrinsics,k)) for k in ('fx','fy','tx','ty')}
                np.savez_compressed(output/'latest-rgbd-private.npz',rgb=rgb,depth=depth)
                snapshots+=1
            if not result['frame_received']:
                result['reason']='Session connected but no RGB-D arrays arrived within the bounded probe'
    except Exception as exc:
        result['error']=type(exc).__name__+': '+str(exc)
    finally:
        try:
            stream.disconnect()
        except Exception:
            pass
        with guard:
            result['frame_callbacks']=len(arrivals)
            if len(arrivals)>1:
                span=arrivals[-1]-arrivals[0]
                result['callback_span_s']=round(span,3)
                result['callback_fps']=round((len(arrivals)-1)/span,3) if span else None
        result['snapshots_read']=snapshots
        result['success']=bool(result['frame_received'] and result['valid_depth_received'] and not result.get('error'))
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
