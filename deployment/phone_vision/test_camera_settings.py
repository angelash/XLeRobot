import json,threading
import numpy as np
import pytest
from deployment.phone_vision.service import PhoneVision
from deployment.phone_vision.worker import orient_frame


def prepared(tmp_path):
    service=PhoneVision(tmp_path,tmp_path/'data')
    service.config_path.parent.mkdir(parents=True)
    service.config_path.write_text(json.dumps({'android_cameras':[{'id':'0','label':'Main'},{'id':'2','label':'Wide'}],
                                             'operator_note':'keep local settings'}),encoding='utf8')
    return service


def test_view_settings_preserve_other_configuration_and_do_not_enable_a_disabled_camera(tmp_path):
    service=prepared(tmp_path)
    result=service.configure_android('2',90)['devices']['android']
    saved=json.loads(service.config_path.read_text('utf8'))
    assert saved['operator_note']=='keep local settings'
    assert saved['camera_id']=='2' and saved['android_rotation']=='90'
    assert result['camera_id']=='2' and result['rotation_deg']==90 and not result['enabled']
    assert list((service.config_path.parent/'phone-vision-backups').glob('view-*.json'))


def test_unconfigured_lens_and_invalid_rotation_cannot_clear_existing_data(tmp_path):
    service=prepared(tmp_path);service.states['android']['enabled']=True
    original=service.config_path.read_bytes()
    for camera,rotation in [('9',0),('2',45),('2',True),('2','90')]:
        with pytest.raises(ValueError):service.configure_android(camera,rotation)
        assert service.states['android']['enabled'] and service.config_path.read_bytes()==original


def test_user_stop_during_switch_cannot_be_undone_by_automatic_resume(tmp_path):
    service=prepared(tmp_path);service.states['android']['enabled']=True
    class Worker:
        def join(self,timeout):
            service.stop('android')
            service.workers.pop('android')
    service.workers['android']={'thread':Worker(),'stop':threading.Event()}
    result=service.configure_android('2',90)['devices']['android']
    assert not result['enabled'] and not service.workers
    assert service.config()['camera_id']=='2'


def test_quarter_turn_preserves_pixel_orientation_and_dimension_metadata():
    original=np.arange(6,dtype=np.uint8).reshape(2,3)
    assert orient_frame(original,90).tolist()==[[3,0],[4,1],[5,2]]
    assert orient_frame(original,270).tolist()==[[2,5],[1,4],[0,3]]
    assert np.array_equal(orient_frame(orient_frame(original,90),270),original)
