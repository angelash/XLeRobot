import pytest
from deployment.phone_vision.android_network import wifi_endpoint,select_device,VideoClock


def test_wifi_selects_only_the_configured_phone_and_retains_authentication_reason():
    target='192.168.88.23:5555'
    assert select_device([['usb-other','device'],[target,'device']],'wifi',target)==(target,None)
    selected,reason=select_device([['usb-other','device'],[target,'unauthorized']],'wifi',target)
    assert selected is None and '允许' in reason
    assert select_device([['usb-other','device']],'wifi',target)[0] is None
    assert select_device([[target,'device']],'usb')[0] is None


@pytest.mark.parametrize('endpoint',['example.org:5555','8.8.8.8:5555','127.0.0.1:5555','192.168.1.2:22','192.168.1.2:5555;reboot','192.168.1.2:05555'])
def test_wifi_endpoint_does_not_accept_public_targets_or_commands(endpoint):
    with pytest.raises(ValueError):wifi_endpoint(endpoint)


def test_source_pts_backlog_cannot_be_relabelled_as_a_fresh_arrival():
    clock=VideoClock()
    assert clock.observe(1_000_000,100.)==(100.,0.)
    acquired,lag=clock.observe(1_100_000,101.2)
    assert acquired==pytest.approx(100.1) and lag==pytest.approx(1.1)
    acquired,lag=clock.observe(2_300_000,101.3)
    assert acquired==pytest.approx(101.3) and lag==pytest.approx(0.)
