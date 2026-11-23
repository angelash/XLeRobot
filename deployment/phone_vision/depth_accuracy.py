"""Tape-measure check of phone depth against a flat target; no robot I/O.

One station is a flat, matte surface filling the centre of the view at a distance the user measured
perpendicular from the phone's front glass. Captures hold private camera images and must stay in an
ignored evidence directory. A report describes this phone, mount, transport and lighting only: its
limits are proposals for review and it never grants motion.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from . import spatial_probe

# Proposed limits per use. Grasp reuses the reviewed 1 cm range threshold in autonomy/protocols.py;
# the other two are new proposals. A station passes when |error| <= max(abs_m, rel * truth).
USES={'grasp':{'range_m':(.25,.70),'abs_m':.01,'rel':0.,'stations':3},
      'near_obstacle':{'range_m':(.30,1.50),'abs_m':.02,'rel':.03,'stations':3},
      'room':{'range_m':(1.50,3.00),'abs_m':0.,'rel':.05,'stations':2}}


def fit_plane(points):
    """Least-squares plane through camera points, refitted once without residuals beyond 3 robust sigmas.

    Returns the unit normal facing the camera, the perpendicular distance from the camera, the kept
    residuals and the kept fraction.
    """
    import numpy as np
    keep=np.ones(len(points),bool)
    for _ in range(2):
        centre=points[keep].mean(axis=0)
        normal=np.linalg.svd(points[keep]-centre,full_matrices=False)[2][-1]
        if normal@centre>0:normal=-normal
        residual=(points-centre)@normal
        sigma=1.4826*np.median(np.abs(residual[keep]-np.median(residual[keep])))
        keep=np.abs(residual)<=max(3*sigma,.002)
    return normal,float(-normal@centre),residual[keep],float(keep.mean())


def camera_points(raw,meta,*,near=.1,far=5.):
    """Depth bytes as an (H, W, 3) camera-coordinate array (x right, y down, z forward) and its valid mask."""
    import numpy as np
    width,height=meta['width'],meta['height']
    if type(width)!=int or type(height)!=int or not 0<width<=4096 or not 0<height<=4096:
        raise ValueError('Invalid depth dimensions')
    if len(raw)!=width*height*4:raise ValueError('Depth byte count mismatch')
    if not 0<near<far<=10:raise ValueError('Invalid depth window')
    k=meta['intrinsics']
    fx,fy,cx,cy=(float(k[n]) for n in ('fx','fy','tx','ty'))
    if not all(np.isfinite([fx,fy,cx,cy])) or min(fx,fy)<=0:raise ValueError('Invalid depth intrinsics')
    z=np.frombuffer(raw,dtype='<f4').reshape(height,width).astype(float)
    valid=np.isfinite(z)&(z>=near)&(z<=far)
    y,x=np.mgrid[0:height,0:width]
    z=np.where(valid,z,0)
    return np.stack(((x-cx)*z/fx,(y-cy)*z/fy,z),axis=-1),valid


def measure(raw,meta,*,window=.3,near=.1,far=5.):
    """Distance from the camera to the flat surface in the central window of one depth frame."""
    import numpy as np
    if not .05<=window<=1:raise ValueError('Invalid measurement window')
    points,valid=camera_points(raw,meta,near=near,far=far)
    height,width=valid.shape
    half_h,half_w=max(2,int(height*window/2)),max(2,int(width*window/2))
    rows,cols=slice(height//2-half_h,height//2+half_h),slice(width//2-half_w,width//2+half_w)
    points,valid=points[rows,cols],valid[rows,cols]
    result={'window_fraction':window,'valid_fraction':float(valid.mean()),'surface_fraction':0.,
            'median_depth_m':None,'plane_distance_m':None,'normal_camera':None,'tilt_deg':None,'plane_rms_m':None}
    if valid.sum()<50:return result
    centre=float(np.median(points[valid][:,2]))
    # A flat target is one surface: readings far from the window median belong to something else.
    surface=valid&(np.abs(points[:,:,2]-centre)<=max(.05,.15*centre))
    normal,distance,residual,kept=fit_plane(points[surface])
    result.update(surface_fraction=float(surface.mean())*kept,median_depth_m=centre,plane_distance_m=abs(distance),
                  normal_camera=normal.tolist(),tilt_deg=float(np.degrees(np.arccos(min(1.,abs(normal[2]))))),
                  plane_rms_m=float(np.sqrt(np.mean(residual**2))))
    return result


def station(truth_m,frames):
    """Median over the frames taken at one known distance; frames without a surface are counted, not hidden."""
    import numpy as np
    if type(truth_m) not in (int,float) or not .05<=truth_m<=10:raise ValueError('truth_m: expected metres in [0.05, 10]')
    seen=[f for f in frames if f['plane_distance_m'] is not None]
    out={'truth_m':float(truth_m),'frames':len(frames),'frames_with_surface':len(seen),'measured_m':None,'error_m':None,
         'repeat_m':None,'tilt_deg':None,'plane_rms_m':None,
         'valid_fraction':float(np.median([f['valid_fraction'] for f in frames])) if frames else 0.}
    if seen:
        values=[f['plane_distance_m'] for f in seen]
        out.update(measured_m=float(np.median(values)),error_m=float(np.median(values)-truth_m),
                   repeat_m=float(max(values)-min(values)),tilt_deg=float(np.median([f['tilt_deg'] for f in seen])),
                   plane_rms_m=float(np.median([f['plane_rms_m'] for f in seen])))
    return out


def evaluate(stations,*,uses=USES,min_valid=.5,max_tilt_deg=20.):
    """Per-use verdicts from tape stations; 'not_tested' means too few stations in that range, not a failure."""
    import numpy as np
    stations=sorted(stations,key=lambda s:s['truth_m'])
    for s in stations:
        s['readable']=bool(s['measured_m'] is not None and s['valid_fraction']>=min_valid and s['tilt_deg']<=max_tilt_deg)
    readable=[s for s in stations if s['readable']]
    report={'stations':stations,'uses':{},'correction':None,'thresholds_status':'proposed_not_reviewed',
            'readable_range_m':[readable[0]['truth_m'],readable[-1]['truth_m']] if readable else None,
            'truth':'user tape measure, perpendicular from the phone front glass to the surface',
            'motion_enabled':False,'robot_commands':0}
    if len(readable)>=3:
        truth=np.array([s['truth_m'] for s in readable]);measured=np.array([s['measured_m'] for s in readable])
        scale,offset=np.polyfit(measured,truth,1)
        report['correction']={'scale':float(scale),'offset_m':float(offset),'stations':len(readable),
                              'rmse_before_m':float(np.sqrt(np.mean((measured-truth)**2))),
                              'rmse_after_m':float(np.sqrt(np.mean((scale*measured+offset-truth)**2))),
                              'note':'Fitted and scored on the same stations; confirm on new distances before use.'}
    for name,rule in uses.items():
        low,high=rule['range_m']
        inside=[s for s in stations if low<=s['truth_m']<=high]
        entry={'range_m':[low,high],'stations':len(inside),'required_stations':rule['stations'],
               'limit':{'abs_m':rule['abs_m'],'rel':rule['rel']}}
        if len(inside)<rule['stations']:entry['verdict']='not_tested'
        else:
            failed=[s['truth_m'] for s in inside
                    if not s['readable'] or abs(s['error_m'])>max(rule['abs_m'],rule['rel']*s['truth_m'])]
            entry.update(verdict='failed' if failed else 'passed',failed_truth_m=failed,
                         max_abs_error_m=max((abs(s['error_m']) for s in inside if s['readable']),default=None))
        report['uses'][name]=entry
    return report


def load(root):
    """Stations saved by `capture`, grouped by depth transport: USB float depth and Wi-Fi hue depth are not mixed."""
    groups={}
    for folder in sorted(p for p in Path(root).iterdir() if (p/'truth.json').is_file()):
        truth=json.loads((folder/'truth.json').read_text(encoding='utf-8'))
        frames=json.loads((folder/'result.json').read_text(encoding='utf-8'))['frames']
        entry=station(truth['truth_m'],[measure((folder/f['files']['raw']['name']).read_bytes(),f['meta']) for f in frames])
        entry['folder']=folder.name
        source=frames[0].get('depth_source','usb_float_depth') if frames else 'unknown'
        groups.setdefault(source,[]).append(entry)
    return groups


def samples(result):
    """Readable stations as learning-ledger samples for protocol P1; the tape value is the user's measurement."""
    return [{'inputs':{'sensor':'phone_depth','depth_source':source,'folder':s.get('folder')},
             'observed':{'measured_distance_m':s['measured_m'],'repeat_m':s['repeat_m'],'tilt_deg':s['tilt_deg'],
                         'plane_rms_m':s['plane_rms_m'],'valid_fraction':s['valid_fraction'],'frames':s['frames']},
             'truth':{'kind':'tape_measure','unit':'m','value':s['truth_m'],'by':'user'}}
            for source,part in result['by_source'].items() for s in part['stations'] if s['readable']]


def report(root):
    result={'root':str(root),'by_source':{source:evaluate(stations) for source,stations in load(root).items()}}
    result['ledger_samples']=samples(result)
    (Path(root)/'depth-accuracy.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def text(result):
    lines=[]
    for source,part in result['by_source'].items():
        lines.append(f'深度来源 {source}：{len(part["stations"])} 个距离')
        for s in part['stations']:
            if s['measured_m'] is None:lines.append(f'  卷尺 {s["truth_m"]*100:6.1f} cm  没有读到平面（{s["frames"]} 帧）');continue
            lines.append(f'  卷尺 {s["truth_m"]*100:6.1f} cm  读数 {s["measured_m"]*100:6.1f} cm  误差 {s["error_m"]*100:+5.1f} cm  '
                         f'有效像素 {s["valid_fraction"]:.0%}  倾斜 {s["tilt_deg"]:.0f}°  帧间差 {s["repeat_m"]*1000:.0f} mm'
                         +('' if s['readable'] else '  不计入'))
        for name,use in part['uses'].items():
            verdict={'passed':'通过','failed':'未通过','not_tested':f'未测够（需 {use["required_stations"]} 个距离）'}[use['verdict']]
            lines.append(f'  用途 {name}（{use["range_m"][0]}–{use["range_m"][1]} m）：{verdict}')
        if part['correction']:
            c=part['correction']
            lines.append(f'  线性修正 真值≈{c["scale"]:.4f}×读数{c["offset_m"]*100:+.1f} cm，均方根误差 '
                         f'{c["rmse_before_m"]*100:.1f}→{c["rmse_after_m"]*100:.1f} cm（同一批数据，需换距离复核）')
    return '\n'.join(lines) or '没有找到带 truth.json 的距离目录'


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    take=commands.add_parser('capture',help='save frames for one tape-measured distance')
    take.add_argument('--truth-cm',type=float,required=True)
    take.add_argument('--root',required=True)
    take.add_argument('--studio',default='http://127.0.0.1:8770')
    take.add_argument('--samples',type=int,default=8)
    take.add_argument('--wait',type=int,default=20)
    show=commands.add_parser('report',help='score every saved distance')
    show.add_argument('--root',required=True)
    show.add_argument('--json',action='store_true')
    args=parser.parse_args(argv)
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8',errors='backslashreplace')
    if args.command=='capture':
        if not 5<=args.truth_cm<=1000:parser.error('--truth-cm: expected 5..1000')
        folder=Path(args.root)/f'station-{round(args.truth_cm*10):05d}mm-{datetime.now():%H%M%S}'
        probe=spatial_probe.capture(args.studio,folder,wait_s=args.wait,samples=args.samples)
        if not probe['ok']:
            print('没有取到新的 iPhone 深度帧：'+str(probe.get('error')));return 2
        (folder/'truth.json').write_text(json.dumps({'truth_m':args.truth_cm/100,'source':'user tape measure',
            'recorded_at':datetime.now().astimezone().isoformat()},ensure_ascii=False),encoding='utf-8')
        print(f'已保存 {len(probe["frames"])} 帧到 {folder.name}')
        return 0
    result=report(args.root)
    print(json.dumps(result,ensure_ascii=False,indent=2) if args.json else text(result))
    return 0


if __name__=='__main__':raise SystemExit(main())
