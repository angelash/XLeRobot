import asyncio
from types import SimpleNamespace

from .phone_source import capture_phone,phone_observation
from .test_local_scan import Rig


def source(**extra):
    return dict(enabled=True,frame_available=True,phase='live',session='session',sequence=2,
                meta={'rgbd':True,'depth_source':'record3d_hue_video','sensor_raw_depth':False},**extra)


def test_iphone_only_is_collected_with_actual_source_and_no_navigation_permission():
    phones={'devices':{'iphone':source(),'android':{'enabled':False}}}
    info=phone_observation(phones)
    assert info['device']=='iphone' and info['frame_available'] and info['rgbd']
    assert info['sensor_raw_depth'] is False and not info['metric_clearance_verified']
    called=[]
    service=SimpleNamespace(status=lambda:phones,frame=lambda device:called.append(device) or 'frame')
    evidence=SimpleNamespace(capture=lambda device,frame,label:(device,frame))
    assert capture_phone(service,evidence)==('iphone','frame') and called==['iphone']


def test_stale_or_disabled_iphone_never_hides_available_android():
    for change in ({'enabled':False},{'frame_available':False}):
        iphone={**source(),**change}
        assert phone_observation({'devices':{'iphone':iphone,'android':source()}})['device']=='android'
    assert phone_observation({'devices':{}})['device'] is None


def test_expiring_frame_falls_back_without_using_old_data():
    called=[]
    def frame(device):
        called.append(device)
        if device=='iphone':raise LookupError('expired')
        return 'new android frame'
    service=SimpleNamespace(status=lambda:{'devices':{'iphone':source(),'android':source()}},frame=frame)
    evidence=SimpleNamespace(capture=lambda device,frame,label:(device,frame))
    assert capture_phone(service,evidence)==('android','new android frame')
    assert called==['iphone','android']


def test_no_frame_never_creates_an_observation():
    def unexpected(*args):raise AssertionError('no stale capture')
    service=SimpleNamespace(status=lambda:{'devices':{'iphone':{'enabled':True,'frame_available':False}}},frame=unexpected)
    assert capture_phone(service,SimpleNamespace(capture=unexpected)) is None


def test_device_switch_creates_separate_epoch_even_with_matching_session_sequence(tmp_path):
    async def run():
        rig=Rig();scan,_=rig.setup(tmp_path)
        scan.state.update(phone_gaps=0,phone_samples=0,phone_epochs=[])
        sample={'device':'android','session':'same-id','sequence':1}
        scan.phone=lambda:dict(sample)
        await scan._phone_once();sample['device']='iphone';await scan._phone_once()
        assert scan.state['phone_samples']==2
        assert [r['device'] for r in scan.state['phone_epochs']]==['android','iphone']
        assert all(not r['registered_with_previous'] for r in scan.state['phone_epochs'])
        await scan._phone_once();assert scan.state['phone_samples']==2
    asyncio.run(run())


def test_cold_camera_warmup_keeps_lease_without_motion_and_obeys_stop(tmp_path):
    async def run():
        rig=Rig();scan,_=rig.setup(tmp_path);rig.failed_views=12
        review=await scan.review()
        assert len(review['photos'])==3 and not rig.steps
        assert all(body is None for _,body in rig.calls)
        # A stop arriving during source warmup must cancel this old request.
        entered=asyncio.Event();release=asyncio.Event()
        original=scan.camera
        async def camera(c):
            entered.set();await release.wait();return await original(c)
        scan.camera=camera
        pending=asyncio.create_task(scan.review());await entered.wait()
        scan.stop_generation+=1;release.set()
        import pytest
        with pytest.raises(ValueError,match='收到停止'):await pending
        assert not rig.steps and not rig.active
    asyncio.run(run())
