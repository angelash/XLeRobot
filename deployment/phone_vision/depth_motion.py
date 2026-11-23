"""Camera pose over the floor, forward free space and base motion read from phone depth; no robot I/O.

Works on frames saved by spatial_probe. Every result depends on a depth source that passed depth_accuracy
and on what one fixed camera can see; it is evidence for review and for the learning ledger, and it never
grants motion. Directions (left/right, closer/farther) are not yet confirmed on the vehicle.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from .depth_accuracy import camera_points,fit_plane,measure

UP_HINT=(0.,-1.,0.)     # camera y points down in an upright phone


def floor_pose(points,valid,*,up_hint=UP_HINT,max_rms_m=.02,expected_height_m=None,height_tolerance_m=.05):
    """Camera height, pitch and roll from the floor plane, or None when no floor is recognisable.

    Planes through three of the steepest downward rays are scored by how many of those rays they explain;
    the best one wins unless a clearly lower plane has at least half its support, so a table top in view is
    not taken for the floor. A known mount height turns a fit that disagrees with it into no floor.
    """
    import numpy as np
    hint=np.asarray(up_hint,float)
    cloud=points[valid]
    if len(cloud)<400:return None
    steep=cloud@hint/np.linalg.norm(cloud,axis=1)
    low=cloud[steep<=np.quantile(steep,.3)]
    rng=np.random.default_rng(0)
    low=low[rng.choice(len(low),min(len(low),4000),replace=False)]
    found=[]
    for a,b,c in low[rng.integers(0,len(low),(300,3))]:
        normal=np.cross(b-a,c-a)
        size=np.linalg.norm(normal)
        if size<1e-9:continue
        normal=normal/size if normal@hint>0 else -normal/size
        height=float(-normal@a)
        if normal@hint<np.cos(np.radians(50)) or height<.05:continue
        found.append((int((np.abs(low@normal+height)<=max(.015,.02*height)).sum()),height,normal))
    if not found:return None
    support,height,normal=max(found,key=lambda f:f[0])
    lower=[f for f in found if f[1]>height+.1 and f[0]>=.5*support]
    if lower:support,height,normal=max(lower,key=lambda f:f[0])
    near=cloud[np.abs(cloud@normal+height)<=max(.02,.03*height)]
    if len(near)<200:return None
    normal,height,residual,kept=fit_plane(near)
    rms=float(np.sqrt(np.mean(residual**2)))
    if normal@hint<np.cos(np.radians(50)) or rms>max_rms_m or height<.05:return None
    if expected_height_m is not None and abs(height-expected_height_m)>height_tolerance_m:return None
    return {'up_camera':normal.tolist(),'height_m':height,'pitch_down_deg':float(np.degrees(np.arcsin(-normal[2]))),
            'roll_deg':float(np.degrees(np.arctan2(normal[0],-normal[1]))),'floor_rms_m':rms,
            'floor_fraction':float(len(near)*kept/len(cloud))}


def level(cloud,pose):
    """Camera points as forward, left and up coordinates with the origin on the floor under the camera."""
    import numpy as np
    up=np.asarray(pose['up_camera'])
    forward=np.array([0.,0.,1.])-up[2]*up
    forward/=np.linalg.norm(forward)
    return cloud@forward,cloud@np.cross(up,forward),cloud@up+pose['height_m']


def clearance(raw,meta,*,half_width_m,camera_left_m=0.,max_height_m=1.3,min_height_m=.03,height_rel=.02,
              cell_m=.1,max_range_m=3.,min_points=6,up_hint=UP_HINT):
    """Free distance ahead inside the body corridor, limited by the nearest obstacle and by how far floor is seen.

    A cell counts as seen floor only when floor points cover the left, middle and right of the corridor, so a
    narrow view or a hidden patch reads as unknown rather than free. Steps down count as obstacles. The floor
    is known free only from blind_to_m to free_to_m; ground nearer than blind_to_m is out of view.
    """
    import numpy as np
    if not .05<=half_width_m<=1 or not .02<=cell_m<=.5 or not .3<=max_range_m<=5 or not 0<min_height_m<max_height_m<=2.5:
        raise ValueError('Invalid corridor')
    points,valid=camera_points(raw,meta,far=max_range_m+2.5)
    pose=floor_pose(points,valid,up_hint=up_hint)
    out={'floor_found':pose is not None,'camera':pose,'blind_to_m':None,'floor_seen_to_m':None,'nearest_obstacle_m':None,
         'obstacle_kind':None,'free_to_m':None,
         'corridor':{'half_width_m':half_width_m,'camera_left_m':camera_left_m,'max_height_m':max_height_m,'cell_m':cell_m},
         'metric_clearance_verified':False,'motion_enabled':False}
    if pose is None:return out
    x,y,z=level(points[valid],pose)
    y=y+camera_left_m
    inside=(x>0)&(x<max_range_m)&(np.abs(y)<=half_width_m)&(z<=max_height_m)
    x,y,z=x[inside],y[inside],z[inside]
    tolerance=np.maximum(min_height_m,height_rel*x)
    kind=np.where(np.abs(z)<tolerance,0,np.where(z>0,1,2))          # floor, raised, drop
    cells=int(np.ceil(max_range_m/cell_m))
    counts=np.zeros((3,cells,3),int)
    np.add.at(counts,(kind,(x/cell_m).astype(int),np.clip(((y+half_width_m)/(2*half_width_m)*3).astype(int),0,2)),1)
    seen=(counts[0]>=min_points).all(axis=1)
    raised,drop=((counts[k]>=min_points).any(axis=1) for k in (1,2))
    blocked=np.flatnonzero(raised|drop)
    if len(blocked):
        out.update(nearest_obstacle_m=float(blocked[0]*cell_m),obstacle_kind='raised' if raised[blocked[0]] else 'drop')
    if seen.any():
        first=int(np.argmax(seen));end=first
        while end<cells and seen[end]:end+=1
        out.update(blind_to_m=float(first*cell_m),floor_seen_to_m=float(end*cell_m))
        limit=min(v for v in (out['floor_seen_to_m'],out['nearest_obstacle_m']) if v is not None)
        if limit>out['blind_to_m']:out['free_to_m']=limit
    return out


def wall(raw,meta,*,window=.4,up_hint=UP_HINT):
    """The flat wall in the centre of one frame, with the floor's up direction when a floor is in view."""
    import numpy as np
    points,valid=camera_points(raw,meta)
    pose=floor_pose(points,valid,up_hint=up_hint)
    if pose:
        # Floor at the foot of the wall has nearly the wall's depth and would tilt the fit.
        depth=np.frombuffer(raw,dtype='<f4').reshape(valid.shape).copy()
        depth[points@np.asarray(pose['up_camera'])+pose['height_m']<.05]=0
        raw=depth.tobytes()
    result=measure(raw,meta,window=window)
    result['up_camera']=pose['up_camera'] if pose else None
    return result


def wall_change(before,after,*,up_hint=UP_HINT):
    """Base motion between two parked views of the same wall.

    yaw_left_deg is the turn of the camera about the vertical; closer_m is the approach along the wall normal;
    travel_m divides that by the cosine of the heading-to-wall angle, which holds only for a straight drive
    along the camera's forward direction.
    """
    import numpy as np
    if before['plane_distance_m'] is None or after['plane_distance_m'] is None:raise ValueError('No wall in view')
    ups=[v['up_camera'] for v in (before,after) if v.get('up_camera')]
    up=np.mean(ups,axis=0) if ups else np.asarray(up_hint,float)
    up/=np.linalg.norm(up)
    flat=[]
    for view in (before,after):
        normal=np.asarray(view['normal_camera'])
        if abs(normal@up)>np.sin(np.radians(25)):raise ValueError('The surface is not a vertical wall')
        normal=normal-(normal@up)*up
        flat.append(normal/np.linalg.norm(normal))
    turn=np.degrees(np.arctan2(up@np.cross(flat[0],flat[1]),flat[0]@flat[1]))
    forward=np.array([0.,0.,1.])-up[2]*up
    forward/=np.linalg.norm(forward)
    facing=float(np.clip(-forward@flat[0],-1,1))
    closer=before['plane_distance_m']-after['plane_distance_m']
    travel=float(closer/facing) if facing>np.cos(np.radians(60)) else None
    # Ready-made learning-ledger truth for W3 and W2; valid only for a depth source that passed P1.
    truth={'yaw':{'kind':'depth_plane','unit':'deg','value':float(-turn)},
           'travel':{'kind':'depth_plane','unit':'m','value':travel} if travel is not None else None}
    return {'yaw_left_deg':float(-turn),'closer_m':float(closer),'heading_to_wall_deg':float(np.degrees(np.arccos(facing))),
            'travel_m':travel,'ledger_truth':truth,
            'up_assumed':not ups,'direction_verified_on_vehicle':False,'motion_enabled':False}


def frames(folder):
    """Raw depth and metadata of every frame a spatial_probe run saved."""
    folder=Path(folder)
    for frame in json.loads((folder/'result.json').read_text(encoding='utf-8'))['frames']:
        yield (folder/frame['files']['raw']['name']).read_bytes(),frame['meta']


def median_view(folder,**options):
    """One wall view from a folder of frames: median distance, mean normal and mean up direction."""
    import numpy as np
    views=[v for v in (wall(raw,meta,**options) for raw,meta in frames(folder)) if v['plane_distance_m'] is not None]
    if not views:raise ValueError(f'No wall in view in {Path(folder).name}')
    ups=[v['up_camera'] for v in views if v['up_camera']]
    return {'plane_distance_m':float(np.median([v['plane_distance_m'] for v in views])),
            'normal_camera':np.mean([v['normal_camera'] for v in views],axis=0).tolist(),
            'up_camera':np.mean(ups,axis=0).tolist() if ups else None,'frames':len(views)}


def corridor(folder,**options):
    """The most cautious clearance over a folder of frames; a frame without a floor makes the answer unknown."""
    results=[clearance(raw,meta,**options) for raw,meta in frames(folder)]
    if not results:raise ValueError('No frames')
    if not all(r['floor_found'] and r['free_to_m'] is not None for r in results):
        return next(r for r in results if not r['floor_found'] or r['free_to_m'] is None)|{'frames':len(results)}
    return min(results,key=lambda r:r['free_to_m'])|{'frames':len(results)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    free=commands.add_parser('clearance',help='camera pose over the floor and free distance ahead')
    free.add_argument('--frames',required=True)
    free.add_argument('--half-width-cm',type=float,required=True)
    free.add_argument('--camera-left-cm',type=float,default=0.)
    free.add_argument('--max-height-cm',type=float,default=130.)
    change=commands.add_parser('change',help='base motion between two parked views of one wall')
    change.add_argument('--before',required=True)
    change.add_argument('--after',required=True)
    args=parser.parse_args(argv)
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8',errors='backslashreplace')
    try:
        if args.command=='clearance':
            result=corridor(args.frames,half_width_m=args.half_width_cm/100,camera_left_m=args.camera_left_cm/100,
                            max_height_m=args.max_height_cm/100)
        else:result=wall_change(median_view(args.before),median_view(args.after))
    except ValueError as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False));return 2
    print(json.dumps({'ok':True}|result,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
