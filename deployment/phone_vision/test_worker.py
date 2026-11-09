"""Run the iPhone worker with generated RGB-D and a fake native stream."""
import io
import sys
import threading
import types
import numpy as np

from deployment.phone_vision.protocol import read_message
from deployment.phone_vision.worker import iphone


def test_iphone_worker_emits_coherent_rgb_depth_and_numeric_array(monkeypatch):
    class Coeffs:
        fx=435.;fy=435.;tx=239.;ty=321.
    class NativeStream:
        def get_connected_devices(self):return [object()]
        def connect(self,device):self.on_new_frame();return True
        def disconnect(self):pass
        def get_rgb_frame(self):return np.full((640,480,3),100,dtype=np.uint8)
        def get_depth_frame(self):return np.full((640,480),.4,dtype=np.float32)
        def get_confidence_frame(self):return np.empty((0,0),dtype=np.uint8)
        def get_intrinsic_mat(self):return Coeffs()
        def get_device_type(self):return 0
    monkeypatch.setitem(sys.modules,'record3d',types.SimpleNamespace(Record3DStream=NativeStream))
    stop=threading.Event()
    class OneFrame(io.BytesIO):
        def flush(self):super().flush();stop.set()
    wire=OneFrame();statuses=[]
    iphone(wire,stop,lambda phase,reason:statuses.append((phase,reason)))
    wire.seek(0);meta,rgb,depth,raw=read_message(wire)
    assert meta['camera_type']==0 and meta['rgbd'] and not meta['calibrated']
    assert meta['width']==480 and meta['height']==640
    assert not meta['confidence_available']
    assert meta['positive_depth_fraction']==1.
    assert np.frombuffer(raw,dtype='<f4').shape==(640*480,)
    assert np.frombuffer(raw,dtype='<f4')[0]==np.float32(.4)
    assert rgb.startswith(b'\xff\xd8') and depth.startswith(b'\xff\xd8')
