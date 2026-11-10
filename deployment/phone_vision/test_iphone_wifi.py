import cv2,numpy as np,pytest
from deployment.phone_vision.iphone_wifi import decode_packed,origin


def test_packed_hue_depth_and_intrinsics_follow_resolution_without_claiming_sensor_precision():
    hsv=np.zeros((4,3,3),dtype=np.float32);hsv[:,:,0]=120.;hsv[:,:,1:]=1.
    encoded=cv2.cvtColor(hsv,cv2.COLOR_HSV2BGR)*255
    color=np.full((4,3,3),77,dtype=np.uint8)
    packed=np.concatenate((encoded.astype(np.uint8),color),axis=1)
    k={'K':[400,0,0,0,400,0,240,320,1],'originalSize':[480,640]}
    rgb,depth,intrinsics=decode_packed(packed,k)
    assert np.array_equal(rgb,color)
    assert np.allclose(depth,1.,atol=.01)
    assert intrinsics=={'fx':2.5,'fy':2.5,'tx':1.5,'ty':2.}
    packed[:,:3]=0
    assert not decode_packed(packed,k)[1].any()
    with pytest.raises(ValueError,match='intrinsics'):decode_packed(packed,{'K':[]})


@pytest.mark.parametrize('value',['https://10.0.0.2','http://8.8.8.8','http://127.0.0.1','http://user:pw@10.0.0.2','http://10.0.0.2/path','http://10.0.0.2?x=1'])
def test_wifi_origin_is_limited_to_explicit_local_phone(value):
    with pytest.raises(ValueError):origin(value)
