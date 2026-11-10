"""Bounded RGB-D capture and camera-coordinate geometry; no robot I/O.

Run with the portable phone Python. Outputs contain private camera captures and
must stay in an ignored evidence directory. A depth cloud is not a registered
room map or a calibrated robot target.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode,urlsplit
from urllib.request import urlopen


def geometry(raw,meta,*,stride=4,near=.15,far=1.5):
    import numpy as np
    width,height=meta['width'],meta['height']
    if type(width)!=int or type(height)!=int or not 0<width<=4096 or not 0<height<=4096:
        raise ValueError('Invalid depth dimensions')
    if len(raw)!=width*height*4:raise ValueError('Depth byte count mismatch')
    if not 1<=stride<=32 or not 0<near<far<=5:raise ValueError('Invalid analysis window')
    k=meta['intrinsics']
    fx,fy,cx,cy=(float(k[n]) for n in ('fx','fy','tx','ty'))
    if not all(np.isfinite([fx,fy,cx,cy])) or min(fx,fy)<=0 or not 0<=cx<width or not 0<=cy<height:
        raise ValueError('Invalid depth intrinsics')
    depth=np.frombuffer(raw,dtype='<f4').reshape(height,width)
    finite=np.isfinite(depth)&(depth>0)
    usable=finite&(depth>=near)&(depth<=far)
    y,x=np.mgrid[0:height:stride,0:width:stride]
    z=depth[::stride,::stride];mask=usable[::stride,::stride]
    points=np.stack(((x-cx)*z/fx,(y-cy)*z/fy,z),axis=-1)[mask]
    region=depth[height//4:3*height//4,width//4:3*width//4]
    valid_region=region[np.isfinite(region)&(region>=near)&(region<=far)]
    summary={'point_count':int(len(points)),'positive_fraction':float(finite.mean()),
             'analysis_fraction':float(usable.mean()),'analysis_window_m':[near,far],
             'center_region_percentiles_m':np.percentile(valid_region,[10,50,90]).tolist() if valid_region.size else None,
             'units':'reported_depth_meters','coordinates':'camera_x_right_y_down_z_forward',
             'calibrated':False,'robot_transform':None,'confidence_available':bool(meta.get('confidence_available')),
             'rgb_depth_registration_verified':False}
    summary['depth_source']=meta.get('depth_source','usb_float_depth')
    summary['sensor_raw_depth']=meta.get('sensor_raw_depth',True)
    return points,summary


def dominant_plane(points,*,threshold=.015,trials=120):
    """Describe the strongest local plane without assuming floor/table orientation."""
    import numpy as np
    if len(points)<100:return None
    rng=np.random.default_rng(240)
    sample=points[rng.choice(len(points),min(6000,len(points)),replace=False)]
    best=None;best_count=0
    for _ in range(trials):
        a,b,c=sample[rng.choice(len(sample),3,replace=False)]
        normal=np.cross(b-a,c-a);length=np.linalg.norm(normal)
        if length<1e-7:continue
        normal/=length;offset=-normal@a
        mask=np.abs(sample@normal+offset)<threshold
        if int(mask.sum())>best_count:best_count=int(mask.sum());best=(normal,offset,mask)
    if best is None or best_count/len(sample)<.3:return None
    normal,offset,mask=best
    centroid=sample[mask].mean(axis=0)
    _,_,vectors=np.linalg.svd(sample[mask]-centroid,full_matrices=False)
    normal=vectors[-1];offset=-normal@centroid
    distance=np.abs(sample@normal+offset);inliers=distance<threshold
    return {'normal_camera':normal.tolist(),'offset_m':float(offset),'threshold_m':threshold,
            'sample_inlier_fraction':float(inliers.mean()),'median_residual_m':float(np.median(distance[inliers])),
            'label':'unclassified_plane','physical_plane_accuracy_verified':False}


def write_ply(path,points):
    with Path(path).open('w',encoding='ascii',newline='\n') as stream:
        stream.write('ply\nformat ascii 1.0\ncomment uncalibrated camera depth; no robot transform\n')
        stream.write(f'element vertex {len(points)}\nproperty float x\nproperty float y\nproperty float z\nend_header\n')
        for x,y,z in points:stream.write(f'{x:.6f} {y:.6f} {z:.6f}\n')


def capture(studio,root,*,wait_s=20,samples=8):
    parsed=urlsplit(studio)
    if parsed.scheme!='http' or parsed.port!=8770 or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        raise ValueError('Use the existing Studio HTTP origin on port 8770')
    if not 1<=wait_s<=60 or not 1<=samples<=60:raise ValueError('Probe budget exceeded')
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    api=studio.rstrip('/')+'/api/platform/phone-vision'
    def get(path):
        with urlopen(api+path,timeout=5) as response:
            return response.read(),{key.lower():value for key,value in response.headers.items()}
    report={'started_at':datetime.now().astimezone().isoformat(),'ok':False,'frames':[],
            'robot_commands':0,'registered_map':False,'robot_transform':None,
            'note':'Single camera depth geometry only. No loop closure, pose tracking or robot target calibration.'}
    deadline=time.monotonic()+wait_s;seen=set()
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            while time.monotonic()<deadline and len(report['frames'])<samples:
                state=json.loads(get('/status')[0]);report['last_status']=state
                phone=state['devices']['iphone'];key=(phone['session'],phone['sequence'])
                if not phone['frame_available'] or key in seen:time.sleep(.2);continue
                query=urlencode({'session':key[0],'seq':key[1]})
                paths={'rgb':f'/iphone/frame.jpg?{query}&kind=rgb',
                       'depth':f'/iphone/frame.jpg?{query}&kind=depth','raw':f'/iphone/depth.raw?{query}'}
                futures={kind:pool.submit(get,path) for kind,path in paths.items()}
                data={kind:future.result() for kind,future in futures.items()}
                for raw,headers in data.values():
                    if headers.get('x-phone-session')!=key[0] or headers.get('x-phone-sequence')!=str(key[1]):
                        raise ValueError('Mixed camera frames rejected')
                points,stats=geometry(data['raw'][0],phone['meta'])
                name=f'{len(report["frames"]):03d}'
                stats.update(session=key[0],sequence=key[1],meta=phone['meta'],source_age_ms_at_query=phone['age_ms'],
                             captured_at=datetime.now().astimezone().isoformat(),files={})
                for kind,(raw,headers) in data.items():
                    filename=f'{name}-{kind}'+('.f32' if kind=='raw' else '.jpg')
                    (root/filename).write_bytes(raw)
                    stats['files'][kind]={'name':filename,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
                if not report['frames']:
                    write_ply(root/'camera-cloud.ply',points);stats['dominant_plane']=dominant_plane(points)
                report['frames'].append(stats);seen.add(key);time.sleep(.25)
        report['ok']=bool(report['frames'])
        if not report['ok']:report['error']='No fresh iPhone RGB-D received within probe budget'
    except Exception as exc:report['error']=type(exc).__name__+': '+str(exc)
    finally:
        report['finished_at']=datetime.now().astimezone().isoformat()
        (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--studio',default='http://127.0.0.1:8770')
    parser.add_argument('--output',required=True)
    parser.add_argument('--wait',type=int,default=20)
    parser.add_argument('--samples',type=int,default=8)
    args=parser.parse_args()
    report=capture(args.studio,args.output,wait_s=args.wait,samples=args.samples)
    print(json.dumps({'ok':report['ok'],'frames':len(report['frames']),'error':report.get('error'),
                      'registered_map':report['registered_map'],'robot_commands':0}))
    raise SystemExit(0 if report['ok'] else 2)


if __name__=='__main__':main()
