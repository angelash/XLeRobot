"""Audit the recorded 2026-11-16 post-reinforcement photographic loop.

Requires post-reinforcement-audit.json from audit_body_spin.py --through 106,
head-optics.json and original exported spin folders. Outputs
body-loop-audit.json. No network, hardware or actuator calls.
Fixed trial range and RGB parameters describe this archived experiment only;
background revisit is not metric SLAM, calibrated yaw or free-space evidence.
"""
from pathlib import Path
import argparse,hashlib,json,math
import cv2,numpy as np
cv2.setNumThreads(1)
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("evidence_root",type=Path)
parser.add_argument("--output",type=Path,help="Defaults to evidence_root/body-loop-audit.json")
args=parser.parse_args()
root=args.evidence_root.resolve(strict=True)
f=float(json.loads((root/'head-optics.json').read_text('utf8'))['focal_px'])
pitch=-math.radians((2350-2220)*360/4096)
orb=cv2.ORB_create(nfeatures=2200);cache={}
def features(item):
    key=str(item['path'])
    if key not in cache:
        a=cv2.imread(key,0);mask=np.zeros(a.shape,np.uint8);mask[:285,:]=255
        k,d=orb.detectAndCompute(a,mask);cache[key]=(a,k,d)
    return cache[key]

def angles(points):return np.arctan2((points[:,0]-319.5)/f,np.cos(pitch)-(239.5-points[:,1])/f*np.sin(pitch))

def compare(a,b):
    ia,ka,da=features(a);ib,kb,db=features(b)
    assert da is not None and db is not None,'no features'
    pairs=cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da,db,k=2);good=[m[0] for m in pairs if len(m)==2 and m[0].distance<.7*m[1].distance]
    assert len(good)>=16,'insufficient matches'
    x=np.float32([ka[m.queryIdx].pt for m in good]);y=np.float32([kb[m.trainIdx].pt for m in good]);_,keep=cv2.findHomography(x,y,cv2.RANSAC,2.5)
    assert keep is not None,'no homography';x=x[keep.ravel()!=0];y=y[keep.ravel()!=0]
    assert len(x)>=12 and np.ptp(x[:,0])>100,'insufficient spread/inliers'
    delta=(angles(y)-angles(x))*180/np.pi;value=float(np.median(delta));mad=float(np.median(abs(delta-value)))
    assert abs(value)<18 and mad<2,'uncertain background motion'
    return {'yaw_deg':value,'mad_deg':mad,'inliers':len(x),'x_span_px':float(np.ptp(x[:,0]))}
audit=json.loads((root/'post-reinforcement-audit.json').read_text('utf8'))
segment=audit['segments'][-1];assert segment['id']=='body-rgb-reference-07'
values=segment['views'];low=min(range(len(values)),key=lambda n:values[n]['yaw_deg'])
loop=values[low:];anchor=loop[0]
assert anchor['source']=='spin-065/after-cam0.jpg' and loop[-1]['source']=='spin-106/after-cam0.jpg'
for view in loop:
    path=(root/view['source']).resolve(strict=True)
    assert path.is_relative_to(root)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==view['source_sha256']
def item(v):return {'path':root/v['source']}
comparisons=[]
for candidate in loop:
    if candidate['yaw_deg']-anchor['yaw_deg'] < 320:
        continue
    row=dict(anchor=anchor['source'],candidate=candidate['source'],
        cumulative_yaw_deg_estimate=candidate['yaw_deg']-anchor['yaw_deg'])
    try:row.update(compare(item(anchor),item(candidate)),ok=True)
    except Exception as exc:row.update(ok=False,error=str(exc))
    comparisons.append(row)
def wheels(s):return {str(m['id']):m['position'] for p in s['ports'] if p['port']=='COM4' for m in p['motors'] if m['id'] in (9,10)}
continuity=[];turns=[]
previous=wheels(json.loads((root/'spin-065/result.json').read_text('utf8'))['final_state'])
for n in range(66,107):
    r=json.loads((root/f'spin-{n:03d}'/'result.json').read_text('utf8'))
    initial=wheels(r['initial']);final=wheels(r['final_state'])
    if previous:
        delta={k:(initial[k]-previous[k]+2048)%4096-2048 for k in initial}
        continuity.append(dict(before_trial=f'spin-{n:03d}',wheel_difference=delta))
    previous=final
    rows=[v for v in r['agent_receipt']['results'] if v['op']=='rotate']
    assert r['kind']=='turn' and r['ok'] and r['health']['stationary_base_verified']
    assert all(v['op'] in ('rotate','snapshot') for v in r['agent_receipt']['results'])
    assert all(v['status']=='done' for v in rows) and r['requested']=='1'
    turns.extend(rows)
assert all(max(abs(v) for v in row['wheel_difference'].values())<=2 for row in continuity)
assert len(turns)==218 and segment['missing_pairs']==0
eligible=[r for r in comparisons if r.get('ok') and r['inliers']>=50 and r['x_span_px']>=100 and r['mad_deg']<.7]
best=min(eligible,key=lambda r:abs(r['yaw_deg']))
assert abs(best['yaw_deg']) < 2
out=dict(reference=segment['id'],anchor=anchor['source'],last=loop[-1]['source'],
    source_frames=len(loop),source_sha256={v['source']:v['source_sha256'] for v in loop},
    cumulative_yaw_deg_estimate=loop[-1]['yaw_deg']-anchor['yaw_deg'],
    comparison_results=comparisons,nearest_background_revisit=best,
    body_full_turn_observed=True,successful_positive_pulses=len(turns),
    wheel_position_continuity=continuity,unexplained_wheel_gap=False,
    translation_commands_during_loop=0,turn_center_drift_independently_measured=False,
    head_clearance_scan='clearance-reinforced-07, no base commands; returned reference retained',
    metric_loop_verified=False,angle_metric_calibrated=False,motion_commands_this_script=0)
(args.output or root/'body-loop-audit.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps({k:v for k,v in out.items() if k not in ('source_sha256','wheel_position_continuity','comparison_results')},ensure_ascii=False))
