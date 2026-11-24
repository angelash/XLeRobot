import json
import numpy as np
import pytest
from deployment.phone_vision import depth_accuracy
from deployment.phone_vision.depth_accuracy import evaluate,measure,station

META={'width':120,'height':160,'intrinsics':{'fx':110.,'fy':110.,'tx':60.,'ty':80.}}


def wall(distance,*,yaw_deg=0.,pitch_deg=0.,meta=META,scale=1.,noise=0.,seed=0):
    """Depth image of a flat wall at a perpendicular distance, turned about the camera's vertical and side axes."""
    yaw,pitch=np.radians([yaw_deg,pitch_deg])
    normal=-np.array([np.sin(yaw)*np.cos(pitch),np.sin(pitch),np.cos(yaw)*np.cos(pitch)])
    k=meta['intrinsics']
    y,x=np.mgrid[0:meta['height'],0:meta['width']]
    rays=np.stack(((x-k['tx'])/k['fx'],(y-k['ty'])/k['fy'],np.ones(x.shape)),axis=-1)
    depth=distance/-(rays@normal)*scale
    if noise:depth=depth+np.random.default_rng(seed).normal(0,noise,depth.shape)
    return depth.astype('<f4')


def test_a_tilted_wall_is_measured_by_its_perpendicular_distance_not_the_centre_pixel():
    result=measure(wall(.8,yaw_deg=12,pitch_deg=-6).tobytes(),META)
    assert result['plane_distance_m']==pytest.approx(.8,abs=1e-4)
    assert result['median_depth_m']>.81
    assert result['tilt_deg']==pytest.approx(np.degrees(np.arccos(np.cos(np.radians(12))*np.cos(np.radians(6)))),abs=.05)
    assert result['normal_camera'][2]<0 and result['valid_fraction']==1


def test_noise_holes_and_a_foreign_object_do_not_move_the_wall():
    depth=wall(1.2,noise=.004,seed=3)
    depth[70:80,50:60]=.4
    depth[82:86,:]=0
    depth[90,40:80]=np.nan
    result=measure(depth.tobytes(),META)
    assert result['plane_distance_m']==pytest.approx(1.2,abs=.002)
    assert .8<result['valid_fraction']<1 and result['surface_fraction']<result['valid_fraction']
    assert result['plane_rms_m']<.006


def test_a_frame_without_usable_depth_reports_no_distance_and_bad_input_is_refused():
    empty=measure(np.zeros((160,120),dtype='<f4').tobytes(),META)
    assert empty['plane_distance_m'] is None and empty['valid_fraction']==0
    with pytest.raises(ValueError,match='byte count'):measure(b'short',META)
    with pytest.raises(ValueError,match='window'):measure(wall(.5).tobytes(),META,window=0)
    with pytest.raises(ValueError,match='intrinsics'):
        measure(wall(.5).tobytes(),META|{'intrinsics':{'fx':0,'fy':1,'tx':1,'ty':1}})
    with pytest.raises(ValueError,match='truth_m'):station(0,[])


def stations(truths,**kind):
    return [station(t,[measure(wall(t,seed=i,**kind).tobytes(),META) for i in range(3)]) for t in truths]


def test_accurate_depth_passes_each_use_that_has_enough_distances():
    result=evaluate(stations([.3,.5,.7,1.,1.4,2.,3.],noise=.001))
    assert {name:use['verdict'] for name,use in result['uses'].items()}=={'grasp':'passed','near_obstacle':'passed','room':'passed'}
    assert result['readable_range_m']==[.3,3.]
    assert result['correction']['scale']==pytest.approx(1,abs=.01) and result['correction']['rmse_before_m']<.002
    assert result['thresholds_status']=='proposed_not_reviewed' and not result['motion_enabled']


def test_a_scale_error_fails_the_uses_it_breaks_and_the_fit_recovers_the_scale():
    result=evaluate(stations([.3,.5,.7,1.,1.4,2.,3.],scale=1.06))
    assert result['uses']['grasp']['verdict']=='failed' and result['uses']['grasp']['failed_truth_m']==[.3,.5,.7]
    assert result['uses']['near_obstacle']['verdict']=='failed' and result['uses']['room']['verdict']=='failed'
    assert result['correction']['scale']==pytest.approx(1/1.06,abs=1e-3)
    assert result['correction']['rmse_after_m']<.001<result['correction']['rmse_before_m']


def test_too_few_distances_is_not_tested_and_an_unreadable_distance_fails_its_range():
    few=evaluate(stations([.5,1.]))
    assert few['uses']['grasp']['verdict']=='not_tested' and few['uses']['room']['verdict']=='not_tested'
    assert few['correction'] is None
    blank=station(2.5,[measure(np.zeros((160,120),dtype='<f4').tobytes(),META)])
    result=evaluate(stations([1.6,2.])+[blank])
    assert blank['frames']==1 and blank['frames_with_surface']==0
    assert result['uses']['room']['verdict']=='failed' and result['uses']['room']['failed_truth_m']==[2.5]
    assert result['readable_range_m']==[1.6,2.]


def stepped(near,far,pitch_deg=-16):
    """Two stacked boxes: the upper half of the view is a face `far`, the lower half a face `near`."""
    depth=wall(near,pitch_deg=pitch_deg);depth[:80,:]=wall(far,pitch_deg=pitch_deg)[:80,:]
    return depth


def test_a_window_across_a_step_is_not_read_as_one_surface():
    # Seen on the vehicle 2026-11-24: a 7 cm step between two boxes gave a low-tilt plane 4 cm off the tape.
    mixed=station(.5,[measure(stepped(.5,.57,pitch_deg=-16).tobytes(),META) for _ in range(3)])
    assert mixed['tilt_deg']<20 and abs(mixed['error_m'])>.03 and mixed['plane_rms_m']>.012
    result=evaluate([mixed]+stations([.3,.7],pitch_deg=-16,noise=.002))
    assert not mixed['readable'] and mixed['unreadable_reason']=='not_one_flat_surface'
    assert [s['truth_m'] for s in result['stations'] if s['readable']]==[.3,.7]
    assert result['uses']['grasp']['verdict']=='failed' and result['uses']['grasp']['failed_truth_m']==[.5]
    assert result['readable_rule']['status']=='proposed_not_reviewed'


def test_range_noise_on_a_far_flat_wall_stays_readable():
    far=station(2.5,[measure(wall(2.5,noise=.01,seed=i).tobytes(),META) for i in range(3)])
    result=evaluate([far])
    assert far['plane_rms_m']>.008 and far['readable'] and result['readable_range_m']==[2.5,2.5]


def save(folder,truth_m,source,depth):
    folder.mkdir(parents=True)
    (folder/'000-raw.f32').write_bytes(depth.tobytes())
    (folder/'result.json').write_text(json.dumps({'frames':[{'meta':META,'depth_source':source,'files':{'raw':{'name':'000-raw.f32'}}}]}))
    (folder/'truth.json').write_text(json.dumps({'truth_m':truth_m}))


def test_report_scores_usb_and_wifi_depth_separately_and_ignores_folders_without_truth(tmp_path,capsys):
    for t in (.3,.5,.7):
        save(tmp_path/f'usb-{t}',t,'usb_float_depth',wall(t))
        save(tmp_path/f'wifi-{t}',t,'record3d_hue_video',wall(t,scale=1.1))
    (tmp_path/'failed-probe').mkdir()
    (tmp_path/'failed-probe/result.json').write_text('{"frames": []}')
    assert depth_accuracy.main(['report','--root',str(tmp_path)])==0
    out=capsys.readouterr().out
    saved=json.loads((tmp_path/'depth-accuracy.json').read_text(encoding='utf-8'))
    assert saved['by_source']['usb_float_depth']['uses']['grasp']['verdict']=='passed'
    assert saved['by_source']['record3d_hue_video']['uses']['grasp']['verdict']=='failed'
    assert '深度来源 usb_float_depth：3 个距离' in out and '未通过' in out and '未测够' in out


def test_capture_records_the_tape_distance_only_when_fresh_frames_arrived(tmp_path,monkeypatch,capsys):
    def probe(studio,folder,*,wait_s,samples):
        folder.mkdir(parents=True)
        return {'ok':studio.endswith('8770'),'frames':[{}]*samples,'error':'No fresh iPhone RGB-D received within probe budget'}
    monkeypatch.setattr(depth_accuracy.spatial_probe,'capture',probe)
    assert depth_accuracy.main(['capture','--truth-cm','50','--root',str(tmp_path/'ok'),'--samples','4'])==0
    folder,=(tmp_path/'ok').iterdir()
    assert folder.name.startswith('station-00500mm-')
    assert json.loads((folder/'truth.json').read_text(encoding='utf-8'))['truth_m']==.5
    assert depth_accuracy.main(['capture','--truth-cm','50','--root',str(tmp_path/'bad'),'--studio','http://127.0.0.1:1'])==2
    assert not list((tmp_path/'bad').glob('*/truth.json'))
    assert '没有取到新的 iPhone 深度帧' in capsys.readouterr().out
    with pytest.raises(SystemExit):depth_accuracy.main(['capture','--truth-cm','2','--root',str(tmp_path)])
