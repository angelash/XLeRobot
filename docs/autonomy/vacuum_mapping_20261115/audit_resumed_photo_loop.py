"""Recompute the 2026-11-17 photographic trajectory and background revisit.

Pure offline image analysis; approximate optics, no metric SLAM, clearance or
actuator permission. Retains failed matches and wheel-reference gaps.
"""
from pathlib import Path
import argparse,hashlib,json,math
import cv2
import numpy as np
cv2.setNumThreads(1)


def run(root,anchor_source):
    progress=json.loads((root/'yaw-progress.json').read_text(encoding='utf8'))
    focal=float(progress['focal_px']);pitch=math.radians(progress['pitch_deg'])
    sources=[progress['pairs'][0]['a']]+[row['b'] for row in progress['pairs']]
    sources=sources[sources.index(anchor_source):]
    orb=cv2.ORB_create(nfeatures=2200);cache={}
    def features(source):
        if source not in cache:
            path=(root/source).resolve(strict=True);assert path.is_relative_to(root)
            image=cv2.imread(str(path),0);mask=np.zeros(image.shape,np.uint8);mask[:285,:]=255
            keypoints,descriptors=orb.detectAndCompute(image,mask)
            cache[source]=(keypoints,descriptors)
        return cache[source]
    def bearings(points):
        return np.arctan2((points[:,0]-319.5)/focal,np.cos(pitch)-(239.5-points[:,1])/focal*np.sin(pitch))
    def compare(a,b):
        ka,da=features(a);kb,db=features(b);assert da is not None and db is not None,'no features'
        pairs=cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da,db,k=2)
        good=[row[0] for row in pairs if len(row)==2 and row[0].distance<.7*row[1].distance]
        assert len(good)>=16,'insufficient matches'
        x=np.float32([ka[m.queryIdx].pt for m in good]);y=np.float32([kb[m.trainIdx].pt for m in good])
        _,keep=cv2.findHomography(x,y,cv2.RANSAC,2.5);assert keep is not None,'no homography'
        x=x[keep.ravel()!=0];y=y[keep.ravel()!=0]
        assert len(x)>=12 and np.ptp(x[:,0])>100,'insufficient spread/inliers'
        delta=(bearings(y)-bearings(x))*180/np.pi;angle=float(np.median(delta));mad=float(np.median(abs(delta-angle)))
        assert abs(angle)<18 and mad<2,'uncertain background motion'
        return {'yaw_deg':angle,'mad_deg':mad,'inliers':len(x),'x_span_px':float(np.ptp(x[:,0]))}
    hashes={source:hashlib.sha256((root/source).read_bytes()).hexdigest() for source in sources}
    angles={sources[0]:0.};total=0.;rows=[];missing=0
    for a,b in zip(sources,sources[1:]):
        row={'a':a,'b':b,'a_sha256':hashes[a],'b_sha256':hashes[b]}
        try:row.update(compare(a,b),ok=True);total+=row['yaw_deg']
        except Exception as exc:row.update(ok=False,error=str(exc));missing+=1
        row['cumulative_deg']=total;angles[b]=total;rows.append(row)
    revisits=[]
    for source,value in angles.items():
        if value<300:continue
        row={'anchor':sources[0],'candidate':source,'cumulative_yaw_deg_estimate':value}
        try:row.update(compare(sources[0],source),ok=True)
        except Exception as exc:row.update(ok=False,error=str(exc))
        revisits.append(row)
    eligible=[row for row in revisits if row.get('ok') and row['inliers']>=50 and row['x_span_px']>=100 and row['mad_deg']<.7]
    best=min(eligible,key=lambda row:abs(row['yaw_deg'])) if eligible else None
    return {'schema_version':1,'anchor':sources[0],'last':sources[-1],'source_frames':len(sources),'source_sha256':hashes,
        'focal_px':focal,'pitch_deg':progress['pitch_deg'],'cumulative_yaw_deg_estimate':total,
        'pairs':rows,'missing_pairs':missing,'comparison_results':revisits,'nearest_background_revisit':best,
        'body_photographic_loop_observed':missing==0 and best is not None and abs(best['yaw_deg'])<2,
        'metric_loop_verified':False,'metric_angle_calibrated':False,'turn_center_drift_measured':False,
        'motion_commands_this_script':0,'scope':'photographic background only; separate control audit retains partial/unknown actions'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path);p.add_argument('--anchor',default='spin-003/before-cam0.jpg');p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists():p.error('Choose a new output file; previous audits are retained')
    result=run(args.root.resolve(strict=True),args.anchor);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:result[k] for k in ('source_frames','cumulative_yaw_deg_estimate','missing_pairs','nearest_background_revisit','body_photographic_loop_observed')},ensure_ascii=False))


if __name__=='__main__':main()
