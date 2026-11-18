"""Visual turn diagnostics and held-out wheel-to-camera gain estimation.

The result corrects a reported angle in a tested scene. It never supplies
clearance, changes wheel geometry/registers, or authorizes an actuator.
"""
from __future__ import annotations
import math
import numpy as np


def color_intrinsics(meta):
    """Return the intrinsics of the preserved JPEG, including SDK crop/rotation."""
    width=meta.get('rgb_width',meta['width']);height=meta.get('rgb_height',meta['height'])
    k=meta.get('rgb_intrinsics')
    if k is None:
        if width==meta['width'] and height==meta['height']:k=meta.get('intrinsics')
        elif meta.get('registration')=='arcore_sdk_texture_to_cpu_crop' and meta.get('rgb_from_depth_scale_offset'):
            sx,sy,ox,oy=meta['rgb_from_depth_scale_offset'];d=meta['intrinsics']
            if min(sx,sy)<=0:raise ValueError('Invalid RGB-depth scale')
            k=dict(fx=d['fx']*sx,fy=d['fy']*sy,tx=d['tx']*sx+ox,ty=d['ty']*sy+oy)
    if not k:raise LookupError('phone_rgb_intrinsics_unavailable')
    k={key:float(k[key]) for key in ('fx','fy','tx','ty')}
    if not np.isfinite(list(k.values())).all() or min(k['fx'],k['fy'])<=0:raise ValueError('Invalid RGB intrinsics')
    return int(width),int(height),k


def rigid_rotation(a,b):
    u,_,vt=np.linalg.svd(a.T@b)
    fix=np.eye(3);fix[2,2]=np.linalg.det(vt.T@u.T)
    return vt.T@fix@u.T


def rgb_turn(first,last,intrinsics):
    import cv2
    if first.shape!=last.shape or first.ndim!=2:raise ValueError('RGB dimensions changed')
    detector=cv2.SIFT_create(nfeatures=2500)
    ka,da=detector.detectAndCompute(first,None);kb,db=detector.detectAndCompute(last,None)
    if da is None or db is None:return {'accepted':False,'reason':'no_descriptors'}
    pairs=cv2.BFMatcher().knnMatch(da,db,k=2)
    matches=[p[0] for p in pairs if len(p)==2 and p[0].distance<.7*p[1].distance]
    if len(matches)<30:return {'accepted':False,'reason':'insufficient_matches','matches':len(matches)}
    x=np.array([ka[m.queryIdx].pt for m in matches]);y=np.array([kb[m.trainIdx].pt for m in matches])
    k=intrinsics;K=np.array([[k['fx'],0,k['tx']],[0,k['fy'],k['ty']],[0,0,1.]])
    if not np.isfinite(K).all() or min(k['fx'],k['fy'])<=0:raise ValueError('Invalid intrinsics')
    def bearings(points):
        b=np.column_stack((points,np.ones(len(points))))@np.linalg.inv(K).T
        return b/np.linalg.norm(b,axis=1)[:,None]
    a,b=bearings(x),bearings(y);rng=np.random.default_rng(20261118)
    threshold=math.radians(.8);best=np.zeros(len(a),bool)
    for _ in range(500):
        pick=rng.choice(len(a),3,replace=False);R=rigid_rotation(a[pick],b[pick])
        angles=np.arccos(np.clip(np.sum((a@R.T)*b,axis=1),-1,1));mask=angles<threshold
        if mask.sum()>best.sum():best=mask
    if best.sum()<30:return {'accepted':False,'reason':'insufficient_rotation_consensus','matches':len(matches),'inliers':int(best.sum())}
    R=rigid_rotation(a[best],b[best]);angles=np.arccos(np.clip(np.sum((a@R.T)*b,axis=1),-1,1))
    span=np.ptp(x[best],axis=0);residual=float(np.degrees(np.median(angles[best])))
    accepted=bool(span[0]>=first.shape[1]*.35 and span[1]>=first.shape[0]*.25 and residual<=.4)
    return dict(accepted=accepted,reason=None if accepted else 'limited_image_support',
                matches=len(matches),inliers=int(best.sum()),inlier_image_extent_px=span.tolist(),
                inlier_median_residual_deg=residual,rotation_matrix=R.tolist(),
                rotation_magnitude_deg=math.degrees(math.acos(np.clip((np.trace(R)-1)/2,-1,1))),
                image_size=[first.shape[1],first.shape[0]],intrinsics=dict(intrinsics),
                method='SIFT_and_RANSAC_unit_bearing_rotation',metric_accuracy_verified=False)


def rotation_vector(matrix):
    import cv2
    R=np.asarray(matrix,dtype=float)
    if R.shape!=(3,3) or not np.isfinite(R).all() or not np.allclose(R.T@R,np.eye(3),atol=1e-4) or abs(np.linalg.det(R)-1)>1e-4:
        raise ValueError('Invalid camera rotation')
    return np.degrees(cv2.Rodrigues(R)[0].reshape(3))


def fit_turn_gain(training,validation):
    """Keep all trials and reject incoherent models; no favourable subset fit."""
    result=dict(accepted=False,training_trials=len(training),validation_trials=len(validation),
                geometry_parameters_written=False,actuator_permission=False,
                registered_robot_map=False,systematic_error_unknown=True,
                reference='camera_rgb_bearing_not_ground_truth')
    rows=training+validation
    if len(training)<6 or len(validation)<2:
        return {**result,'reason':'need_six_training_and_two_independent_validation_trials'}
    if len({r.get('id') for r in rows})!=len(rows) or None in {r.get('id') for r in rows}:
        return {**result,'reason':'duplicate_or_missing_trial_identity'}
    reference=rows[0].get('reference')
    if not reference or any(r.get('reference')!=reference for r in rows):
        return {**result,'reason':'source_or_controller_reference_changed'}
    if any(not r.get('native_completed') or not r.get('parked_verified') for r in rows):
        return {**result,'reason':'failed_or_uncertain_physical_trial'}
    if any(not r.get('visual',{}).get('accepted') for r in rows):
        return {**result,'reason':'visual_evidence_rejected'}
    design=np.asarray([r.get('design_angle_deg',math.nan) for r in rows],float)
    if not np.isfinite(design).all() or np.any(np.abs(design)<3) or np.any(np.abs(design)>60):
        return {**result,'reason':'invalid_design_angle_or_trial_envelope'}
    n=len(training)
    if any(np.count_nonzero(np.sign(design[:n])==s)<3 or np.count_nonzero(np.sign(design[n:])==s)<1 for s in (-1,1)):
        return {**result,'reason':'both_directions_required_in_training_and_validation'}
    try:vectors=np.array([rotation_vector(r['visual']['rotation_matrix']) for r in rows])
    except (ValueError,KeyError):return {**result,'reason':'invalid_rotation_matrix'}
    angles=np.linalg.norm(vectors,axis=1)
    if np.any(angles<2) or np.any(angles>35):return {**result,'reason':'visual_turn_outside_test_envelope'}
    axes=vectors/angles[:,None]*np.sign(design)[:,None]
    _,_,vt=np.linalg.svd(axes[:n],full_matrices=False);axis=vt[0]
    if np.dot(axis,axes[0])<0:axis=-axis
    deviations=np.degrees(np.arccos(np.clip(axes@axis,-1,1)))
    if np.any(deviations>10):return {**result,'reason':'camera_rotation_axis_inconsistent','axis_deviation_deg':deviations.tolist()}
    measured=vectors@axis;ratios=measured[:n]/design[:n]
    gain=float(np.median(ratios));mad=float(np.median(np.abs(ratios-gain)))
    if not .2<=gain<=1.5 or mad/gain>.08:
        return {**result,'reason':'gain_incoherent','gain':gain,'ratio_mad':mad}
    predicted=design*gain;error=np.abs(predicted-measured)
    limits=np.maximum(1.5,np.abs(measured)*.15)
    by_trial=[dict(id=r['id'],design_deg=float(d),visual_deg=float(v),corrected_deg=float(p),
                   error_deg=float(e),limit_deg=float(l),role='fit' if i<n else 'validation')
              for i,(r,d,v,p,e,l) in enumerate(zip(rows,design,measured,predicted,error,limits))]
    result.update(gain=gain,ratio_mad=mad,camera_axis=axis.tolist(),axis_deviation_deg=deviations.tolist(),
                  trials=by_trial,scope=dict(reference=reference,design_angle_abs_range_deg=[float(abs(design).min()),float(abs(design).max())]))
    if np.any(error>limits):return {**result,'reason':'training_or_held_out_error_exceeds_limit'}
    return {**result,'accepted':True,'reason':'both_direction_held_out_rotation_checks_passed',
            'maximum_validation_error_deg':float(error[n:].max()),
            'estimated_gain_sampling_error':1.4826*mad/math.sqrt(n),
            'limits_note':'A scene-tested angular estimate only; no physical wheel radius/track or clearance is identified.'}
