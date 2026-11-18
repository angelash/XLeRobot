"""Recompute private iPhone depth/rotation evidence, without robot I/O.

RGB bearing alignment is a diagnostic rotation model, not ground truth or a
sensor-to-base calibration. The original RGB-D files remain unchanged.
"""
from pathlib import Path
import argparse,hashlib,json,math
import cv2
import numpy as np


def rotation(a,b):
    u,_,vt=np.linalg.svd(a.T@b)
    correction=np.eye(3);correction[2,2]=np.linalg.det(vt.T@u.T)
    return vt.T@correction@u.T


def rgb_bearing_rotation(first,last,intrinsics):
    sift=cv2.SIFT_create(nfeatures=2500)
    ka,da=sift.detectAndCompute(first,None);kb,db=sift.detectAndCompute(last,None)
    if da is None or db is None:return {'accepted':False,'reason':'no_descriptors'}
    pairs=cv2.BFMatcher().knnMatch(da,db,k=2)
    matches=[p[0] for p in pairs if len(p)==2 and p[0].distance<.7*p[1].distance]
    if len(matches)<15:return {'accepted':False,'reason':'insufficient_matches','matches':len(matches)}
    x=np.array([ka[m.queryIdx].pt for m in matches]);y=np.array([kb[m.trainIdx].pt for m in matches])
    k=intrinsics;K=np.array([[k['fx'],0,k['tx']],[0,k['fy'],k['ty']],[0,0,1.]])
    def bearings(points):
        b=np.column_stack((points,np.ones(len(points))))@np.linalg.inv(K).T
        return b/np.linalg.norm(b,axis=1)[:,None]
    a,b=bearings(x),bearings(y);rng=np.random.default_rng(20261118)
    threshold=math.radians(.8);best=np.zeros(len(a),bool)
    for _ in range(500):
        pick=rng.choice(len(a),3,replace=False);R=rotation(a[pick],b[pick])
        angles=np.arccos(np.clip(np.sum((a@R.T)*b,axis=1),-1,1));mask=angles<threshold
        if mask.sum()>best.sum():best=mask
    if best.sum()<15:return {'accepted':False,'reason':'insufficient_rotation_consensus','matches':len(matches),'inliers':int(best.sum())}
    R=rotation(a[best],b[best]);angles=np.arccos(np.clip(np.sum((a@R.T)*b,axis=1),-1,1))
    extent=np.ptp(x[best],axis=0)
    return {'accepted':True,'matches':len(matches),'inliers':int(best.sum()),
            'inlier_image_extent_px':extent.tolist(),
            'rotation_magnitude_deg':math.degrees(math.acos(np.clip((np.trace(R)-1)/2,-1,1))),
            'rotation_matrix':R.tolist(),'inlier_median_residual_deg':float(np.degrees(np.median(angles[best]))),
            'threshold_deg':.8,'method':'SIFT_ratio_matches_and_RANSAC_unit_bearing_rotation',
            'limits':'Same RGB source; translation, distortion and dynamics may bias this rotation-only fit. Not ground truth.'}


def audit(folder):
    rows=json.loads((folder/'frames.json').read_text('utf8'))['frames'];stats=[]
    for row in rows:
        for kind in ('rgb','depth'):
            name=row[kind+'_file'];assert Path(name).name==name
            raw=(folder/name).read_bytes();assert hashlib.sha256(raw).hexdigest()==row[kind+'_sha256']
        a=np.frombuffer((folder/row['depth_file']).read_bytes(),dtype='<f4')
        assert len(a)==row['meta']['width']*row['meta']['height']
        valid=np.isfinite(a)&(a>0)
        stats.append({'sequence':row['sequence'],'positive_fraction':float(valid.mean()),
                      'in_02_3m_fraction':float(((a>=.2)&(a<=3)).mean()),
                      'positive_median_m':float(np.median(a[valid])) if valid.any() else None})
    assert len({(r['session'],r['sequence']) for r in rows})==len(rows)
    assert len({r['session'] for r in rows})==1
    first,last=rows[0],rows[-1]
    image=lambda r:cv2.imread(str(folder/r['rgb_file']),cv2.IMREAD_GRAYSCALE)
    visual=rgb_bearing_rotation(image(first),image(last),first['meta']['intrinsics'])
    return {'frames':len(rows),'payload_hashes_verified':len(rows)*2,'depth_stats':stats,
            'rgb_rotation':visual,'geometry_adopted':False,'metric_accuracy_verified':False,
            'registered_robot_map':False,'navigation_validated':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--evidence-dir',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=audit(args.evidence_dir)
    with args.output.open('x',encoding='utf8') as f:json.dump(result,f,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({k:v for k,v in result.items() if k!='depth_stats'},ensure_ascii=False))


if __name__=='__main__':main()
