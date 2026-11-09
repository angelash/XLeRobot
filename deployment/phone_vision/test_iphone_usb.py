"""Depth validity checks with generated values; no iPhone access."""
import numpy as np
import pytest

from deployment.phone_vision.iphone_usb import depth_summary


def test_zero_negative_and_nonfinite_depth_do_not_count_as_measurements():
    depth=np.array([[0.,-1.,np.nan,np.inf],[.2,.3,.4,.5]],dtype=np.float32)
    result=depth_summary(depth)
    assert result['valid_positive_depth_fraction']==.5
    assert result['valid_positive_depth_pixels']==4
    assert result['positive_depth_range_m']==pytest.approx([.2,.5])
    assert result['positive_depth_p05_p50_p95_m'][1]==pytest.approx(.35)


def test_empty_or_entirely_invalid_depth_has_no_distance_summary():
    for depth in (np.array([],dtype=np.float32),np.array([0.,np.nan,-.5])):
        result=depth_summary(depth)
        assert result['valid_positive_depth_fraction']==0
        assert result['valid_positive_depth_pixels']==0
        assert 'positive_depth_range_m' not in result
