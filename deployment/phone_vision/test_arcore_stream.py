import struct
import cv2,numpy as np
import pytest
from deployment.phone_vision.arcore_stream import prepare, packet_age_seconds, DeviceClock, read_packet


def test_native_mm_depth_is_converted_and_rotated_with_scaled_camera_intrinsics():
    color=np.full((4,6,3),70,dtype=np.uint8);ok,jpeg=cv2.imencode('.jpg',color);assert ok
    meta={'depth_width':3,'depth_height':2,'image_dimensions':[6,4],
          'focal_length':[6.,4.],'principal_point':[3.,2.],'tracking':'TRACKING',
          'capture_session':'sample','frame_timestamp_ns':100,'depth_timestamp_ns':100,
          'rgb_timestamp_ns':100,'raw_depth_timestamp_ns':100,
          'depth_registration':'arcore_texture_normalized_to_cpu_image',
          'depth_uv_to_image_pixels':[0,0,6,0,0,4,6,4],
          'pose_translation':[0,0,0],'pose_quaternion':[0,0,0,1]}
    raw=struct.pack('<6H',1000,0,1000,2000,2000,2000)
    details,rgb,preview,array=prepare(meta,jpeg.tobytes(),raw,b'',bytes([255]*6),90)
    assert (details['width'],details['height'])==(2,3)
    assert details['intrinsics']=={'fx':2.,'fy':3.,'tx':0.,'ty':1.5}
    assert np.allclose(np.frombuffer(array,dtype='<f4').reshape(3,2),[[2.,1.],[2.,0.],[2.,1.]])
    assert not details['sensor_raw_depth'] and not details['confidence_available']
    assert details['pose_coordinates']=='arcore_world_opengl_not_robot_frame'


def sample():
    color=np.zeros((8,8,3),dtype=np.uint8)
    color[:2]=5;color[2:6]=170;color[6:]=250
    _,jpeg=cv2.imencode('.jpg',color,[cv2.IMWRITE_JPEG_QUALITY,100])
    meta={'depth_width':4,'depth_height':2,'image_dimensions':[8,8],
          'focal_length':[8.,8.],'principal_point':[4.,4.],'tracking':'TRACKING',
          'capture_session':'test','frame_timestamp_ns':987654321,'rgb_timestamp_ns':1_000_000_000,
          'depth_timestamp_ns':1_000_000_000,'pose_translation':[0,0,0],'pose_quaternion':[0,0,0,1],
          'host_device_monotonic_ms':30_000,'depth_registration':'arcore_texture_normalized_to_cpu_image',
          'depth_uv_to_image_pixels':[0,2,8,2,0,6,8,6]}
    return meta,jpeg.tobytes(),struct.pack('<8H',*([1500]*8))


def test_sdk_crop_matches_depth_instead_of_stretching_full_cpu_image():
    meta,jpeg,raw=sample()
    details,color,_,_=prepare(meta,jpeg,raw,b'',b'',0)
    decoded=cv2.imdecode(np.frombuffer(color,dtype=np.uint8),cv2.IMREAD_COLOR)
    assert np.allclose(decoded,170,atol=2)
    assert details['intrinsics']=={'fx':4.,'fy':4.,'tx':2.,'ty':1.}


def test_fresh_rgb_does_not_refresh_cached_depth():
    meta,jpeg,raw=sample();meta['rgb_timestamp_ns']+=2_000_000_000
    details,_,preview,values=prepare(meta,jpeg,raw,b'',b'',0)
    assert not details['rgbd'] and not preview and not values
    assert details['depth_age_ms']==2000 and '过期' in details['depth_unavailable_reason']
    assert (details['width'],details['height'])==(8,8)
    # Frame's timestamp clock is undefined; packet age uses elapsedRealtime.
    assert packet_age_seconds(meta,30.25)==.25


def test_sdk_mapping_is_required_and_unfamiliar_reflections_rejected():
    meta,jpeg,raw=sample();meta.pop('depth_registration')
    with pytest.raises(ValueError,match='SDK坐标'):prepare(meta,jpeg,raw,b'',b'',0)
    meta['depth_registration']='arcore_texture_normalized_to_cpu_image'
    meta['depth_uv_to_image_pixels']=[8,2,0,2,8,6,0,6]
    with pytest.raises(ValueError,match='相机轴裁剪'):prepare(meta,jpeg,raw,b'',b'',0)


def test_paused_tracking_still_provides_rgb_without_depth_or_valid_pose():
    meta,jpeg,raw=sample();meta['tracking']='PAUSED';meta['tracking_failure']='INSUFFICIENT_LIGHT'
    details,_,preview,values=prepare(meta,jpeg,raw,b'',b'',90)
    assert not details['rgbd'] and not details['pose_valid'] and not preview and not values


def test_all_zero_depth_is_rgb_only_instead_of_a_ready_depth_stream():
    meta,jpeg,raw=sample()
    details,_,preview,values=prepare(meta,jpeg,bytes(len(raw)),b'',b'',0)
    assert not details['rgbd'] and not preview and not values and '暂无有效距离' in details['depth_unavailable_reason']


def test_device_clock_keeps_asymmetric_network_uncertainty():
    clock=DeviceClock();clock.observe(100.,30.,100.4)
    # Actual offset may be anywhere in [69.99,70.4]; select the oldest source.
    assert clock.acquired_floor(30_200,100.5)<=100.19
    clock.observe(101.,31.1,101.2)
    assert clock.floor<=70.05<=clock.ceiling
    assert clock.acquired_floor(31_400,101.6)<=101.45


def test_native_stream_handles_fragmented_packets_timeouts_and_bounds():
    import json,socket,threading
    from deployment.phone_vision.arcore_capture import HEADER
    data=json.dumps({'schema':1,'capture_session':'generated'}).encode()
    raw=HEADER.pack(b'XLAR1\0\0\0',len(data),0,0,0,0)+data
    class Connection:
        def __init__(self,data):self.data=data;self.calls=0
        def recv(self,n):
            self.calls+=1
            if self.calls==3:raise socket.timeout()
            out=self.data[:min(n,7)];self.data=self.data[len(out):];return out
    assert read_packet(Connection(raw),threading.Event())[0]['capture_session']=='generated'
    with pytest.raises(ValueError,match='bounds'):
        read_packet(Connection(HEADER.pack(b'XLAR1\0\0\0',100_000,0,0,0,0)),threading.Event())
