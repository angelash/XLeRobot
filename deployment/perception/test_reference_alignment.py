"""Generated image cases; no private camera images or robot access."""
import cv2
import numpy as np
import pytest
from deployment.perception.reference_alignment import compare


def texture():
    rng = np.random.default_rng(19)
    image = np.zeros((240, 320), dtype=np.uint8)
    for _ in range(160):
        x, y = rng.integers([15, 15], [300, 220])
        cv2.circle(image, (int(x), int(y)), int(rng.integers(2, 8)), int(rng.integers(60, 255)), -1)
    return cv2.GaussianBlur(image, (3, 3), 0)


def test_known_pixel_translation_is_reported_without_motion_authority():
    a = texture()
    b = cv2.warpAffine(a, np.float32([[1, 0, 7], [0, 1, -4]]), (320, 240))
    r = compare(a, b, [20, 20, 295, 215])
    assert r['usable'] and r['inliers'] >= 8
    assert r['median_shift_px'] == pytest.approx([7, -4], abs=.35)
    assert r['roi_center_shift_px'] == pytest.approx([7, -4], abs=.35)
    assert r['action_authority'] is False and r['coordinate_system'] == 'image_pixels_not_metric_pose'


def test_blank_or_obscured_reference_does_not_become_a_success():
    a = texture();blank = np.zeros_like(a)
    assert not compare(a, blank, [20, 20, 295, 215])['usable']
    assert compare(blank, blank, [20, 20, 295, 215])['reason'] == 'insufficient_texture'


@pytest.mark.parametrize('roi', [[-1, 0, 200, 100], [0, 0, 400, 100], [20, 20, 10, 100],
                                 [0, 0, 10, 10], [0, 0, 200, True], [0, 0, 200]])
def test_invalid_roi_is_rejected(roi):
    a = texture()
    with pytest.raises(ValueError):
        compare(a, a, roi)


def test_mismatched_images_are_rejected():
    a = texture()
    with pytest.raises(ValueError, match='dimensions'):
        compare(a, a[:100], [20, 20, 95, 95])
    with pytest.raises(ValueError, match='uint8'):
        compare(a.astype(float), a, [20, 20, 95, 95])
