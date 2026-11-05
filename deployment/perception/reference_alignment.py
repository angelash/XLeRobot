"""Read-only image-reference alignment for supervised repeatability checks.

Use the same camera/pose and a static textured ROI. Image alignment does not
measure ground distance, chassis yaw, clearance, or permission to move.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def compare(reference, current, roi):
    """Match a static image region and report pixel displacement, never actions."""
    def gray(frame):
        if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.size == 0:
            raise ValueError('Need nonempty uint8 camera images')
        if frame.ndim == 2:
            return frame
        if frame.ndim == 3 and frame.shape[2] == 3:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        raise ValueError('Need grayscale or BGR camera images')

    a, b = gray(reference), gray(current)
    if a.shape != b.shape:
        raise ValueError('Reference and current image dimensions must match')
    if not isinstance(roi, (list, tuple)) or len(roi) != 4 or any(type(v) is not int for v in roi):
        raise ValueError('ROI must contain four integer coordinates')
    x0, y0, x1, y1 = roi
    h, w = a.shape
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h and min(x1-x0, y1-y0) >= 24):
        raise ValueError('ROI must stay inside both images and span at least 24 pixels')
    mask = np.zeros_like(a)
    mask[y0:y1, x0:x1] = 255
    sift = cv2.SIFT_create()
    ka, da = sift.detectAndCompute(a, mask)
    kb, db = sift.detectAndCompute(b, mask)
    result = {'method': 'sift_ransac_reference_v1', 'action_authority': False,
              'coordinate_system': 'image_pixels_not_metric_pose', 'roi': list(roi),
              'usable': False, 'matches': 0, 'inliers': 0,
              'requires': 'same camera and pose; static textured ROI; supervisor review'}
    if da is None or db is None:
        return {**result, 'reason': 'insufficient_texture'}
    pairs = cv2.BFMatcher().knnMatch(da, db, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < .7*p[1].distance]
    result['matches'] = len(good)
    if len(good) < 8:
        return {**result, 'reason': 'insufficient_matches'}
    pa = np.float32([ka[m.queryIdx].pt for m in good])
    pb = np.float32([kb[m.trainIdx].pt for m in good])
    homography, inliers = cv2.findHomography(pa, pb, cv2.RANSAC, 3)
    if homography is None or inliers is None or not np.isfinite(homography).all():
        return {**result, 'reason': 'no_robust_alignment'}
    keep = inliers.ravel().astype(bool)
    result['inliers'] = int(keep.sum())
    if result['inliers'] < 8 or result['inliers']/len(good) < .5:
        return {**result, 'reason': 'insufficient_inliers'}
    delta = pb[keep]-pa[keep]
    center = np.float32([[[(x0+x1)/2, (y0+y1)/2]]])
    projected = cv2.perspectiveTransform(center, homography)
    if not np.isfinite(projected).all():
        return {**result, 'reason': 'invalid_projection'}
    return {**result, 'usable': True, 'reason': None,
            'median_shift_px': np.median(delta, axis=0).tolist(),
            'roi_center_shift_px': (projected-center)[0, 0].tolist(),
            'median_point_distance_px': float(np.median(np.linalg.norm(delta, axis=1))),
            'homography': homography.tolist()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference', type=Path)
    parser.add_argument('current', type=Path)
    parser.add_argument('--roi', type=int, nargs=4, required=True, metavar=('X0', 'Y0', 'X1', 'Y1'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    a, b = cv2.imread(str(args.reference)), cv2.imread(str(args.current))
    if a is None or b is None:
        parser.error('Both image files must decode successfully')
    result = compare(a, b, args.roi)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text+'\n', encoding='utf-8')
    print(text)
    return 0 if result['usable'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
