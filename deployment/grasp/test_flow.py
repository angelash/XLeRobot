"""Identity-chain regression: independent anchors must not inherit NCC drift."""
import cv2
import numpy as np

from deployment.grasp.flow import FeatureContinuity


def fixture():
    image = np.full((480, 640), 220, np.uint8)
    image[170:330, 240:400] = np.random.default_rng(51).integers(30, 170, (160,160), dtype=np.uint8)
    return image, [240,170,160,160]


def test_frozen_identity_survives_small_rotation_without_reseeding():
    image, box = fixture()
    flow = FeatureContinuity(); flow.init(image, box)
    for step in range(1, 16):
        transform = cv2.getRotationMatrix2D((320,250), step*.3, 1+step*.002)
        transform[:,2] += (step*.4, step*.2)
        result = flow.update(cv2.warpAffine(image,transform,(640,480),borderValue=220))
        assert result['ok']
        assert result['anchor_scale'] == __import__('pytest').approx(1+step*.002, abs=.015)
    assert len(flow.points) <= 100


def test_gradual_shrink_cannot_bypass_anchor_scale_guard():
    image, box = fixture()
    flow = FeatureContinuity(); flow.init(image,box)
    results = []
    for step in range(1, 30):
        scale = .975**step
        transform = cv2.getRotationMatrix2D((320,250),0,scale)
        results.append(flow.update(cv2.warpAffine(image,transform,(640,480),borderValue=220)))
    assert results[0]['ok']
    assert any(not row['ok'] for row in results)
    assert not results[-1]['ok']
    assert not flow.update(image)['ok'], 'A failed chain cannot spontaneously reacquire'


def test_only_a_local_texture_fragment_is_not_complete_object_evidence():
    image, box = fixture()
    flow = FeatureContinuity(); flow.init(image,box)
    fragment = np.full_like(image,220)
    fragment[230:265,300:335] = image[230:265,300:335]
    assert not flow.update(fragment)['ok']
    assert not flow.update(image)['ok']
