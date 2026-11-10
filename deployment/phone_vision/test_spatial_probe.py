import numpy as np
import pytest
from deployment.phone_vision.spatial_probe import geometry,dominant_plane


def test_analytic_plane_projection_preserves_scale_and_rejects_invalid_depth():
    depth=np.ones((40,40),dtype='<f4')*.5
    depth[0,0]=float('nan');depth[0,1]=0;depth[0,2]=8
    meta={'width':40,'height':40,'intrinsics':{'fx':40,'fy':40,'tx':20,'ty':20}}
    points,summary=geometry(depth.tobytes(),meta,stride=1)
    assert len(points)==1597
    assert any(np.allclose(point,[0,0,.5]) for point in points)
    assert summary['center_region_percentiles_m']==[.5,.5,.5]
    plane=dominant_plane(points)
    assert abs(abs(plane['normal_camera'][2])-1)<1e-6
    assert abs(abs(plane['offset_m'])-.5)<1e-6
    assert plane['sample_inlier_fraction']==1
    assert summary['robot_transform'] is None and not summary['calibrated']
    with pytest.raises(ValueError,match='byte count'):geometry(b'bad',meta)
    meta['intrinsics']['fx']=0
    with pytest.raises(ValueError,match='intrinsics'):geometry(depth.tobytes(),meta)


def test_capture_consumes_the_same_fresh_frame_with_http_header_case(tmp_path,monkeypatch):
    import io,json
    from deployment.phone_vision import spatial_probe
    meta={'width':40,'height':40,'intrinsics':{'fx':40,'fy':40,'tx':20,'ty':20}}
    state={'devices':{'iphone':{'session':'current','sequence':7,'frame_available':True,'meta':meta,'age_ms':10}}}
    calls=[]
    class Response(io.BytesIO):
        headers={'x-phone-session':'current','x-phone-sequence':'7'}
    def get(url,timeout):
        calls.append(url)
        if url.endswith('/status'):return Response(json.dumps(state).encode())
        assert 'session=current&seq=7' in url
        return Response((np.ones((40,40),dtype='<f4')*.5).tobytes() if 'depth.raw' in url else b'generated jpeg')
    monkeypatch.setattr(spatial_probe,'urlopen',get)
    report=spatial_probe.capture('http://127.0.0.1:8770',tmp_path/'run',wait_s=1,samples=1)
    assert report['ok'] and len(report['frames'])==1
    assert report['robot_commands']==0 and not report['registered_map']
    assert (tmp_path/'run/camera-cloud.ply').is_file()
    assert len(calls)==4
