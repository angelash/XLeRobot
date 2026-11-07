"""Local feature continuity evidence; no robot commands or semantic labels."""
from __future__ import annotations

import cv2
import numpy as np


class FeatureContinuity:
    """Forward/backward KLT and a bounded rigid image transform.

    Intended as extra evidence when a known object's appearance changes as
    fingers close. A point cluster, vanished object or implausible jump cannot
    authorize a new detection. Sizes and motion limits are image-space only.
    """
    def __init__(self):
        self.previous = self.points = self.corners = None
        self.anchor_points = self.anchor_corners = None

    def init(self, gray, box):
        x, y, w, h = box
        mask = np.zeros(gray.shape, np.uint8)
        x0, y0 = max(0, int(x+2)), max(0, int(y+2))
        x1, y1 = min(gray.shape[1], int(x+w-2)), min(gray.shape[0], int(y+h-2))
        mask[y0:y1, x0:x1] = 255
        self.points = cv2.goodFeaturesToTrack(gray, maxCorners=100, qualityLevel=.015,
                                             minDistance=4, mask=mask, blockSize=5)
        self.previous = gray.copy()
        self.corners = np.array([[x,y],[x+w,y],[x+w,y+h],[x,y+h]], np.float32)
        self.anchor_corners = self.corners.copy()
        self.anchor_points = None if self.points is None else self.points.copy()

    def update(self, gray):
        if self.points is None or len(self.points) < 8:
            return {'ok':False, 'why':'insufficient distributed features'}
        new, status, _ = cv2.calcOpticalFlowPyrLK(self.previous, gray, self.points, None,
                                                winSize=(21,21), maxLevel=3)
        if new is None:
            self.points = None
            return {'ok':False, 'why':'flow unavailable'}
        back, reverse, _ = cv2.calcOpticalFlowPyrLK(gray, self.previous, new, None,
                                                  winSize=(21,21), maxLevel=3)
        if back is None:
            self.points = None
            return {'ok':False, 'why':'reverse flow unavailable'}
        good = (status.ravel()>0)&(reverse.ravel()>0)&(np.linalg.norm(back-self.points,axis=2).ravel()<1.5)
        a, b = self.points[good].reshape(-1,2), new[good].reshape(-1,2)
        original = self.anchor_points[good].reshape(-1,2)
        self.previous = gray.copy()
        self.points = None
        if len(a) < 8:
            return {'ok':False, 'why':'too few reversible features'}
        # Fit the same original feature identities, never a freshly seeded
        # NCC box. Incremental fits alone can slowly shrink onto a jaw edge.
        transform, inliers = cv2.estimateAffinePartial2D(original,b,method=cv2.RANSAC,ransacReprojThreshold=2.)
        if transform is None or inliers is None:
            return {'ok':False, 'why':'transform unavailable'}
        use = inliers.ravel().astype(bool)
        x,y,w,h = cv2.boundingRect(self.corners)
        spread = np.ptp(a[use],axis=0) if use.any() else (0,0)
        anchor_spread = np.ptp(original[use],axis=0) if use.any() else (0,0)
        _,_,aw,ah = cv2.boundingRect(self.anchor_corners)
        cumulative_scale = float(np.hypot(transform[0,0],transform[1,0]))
        previous_scale = float(np.linalg.norm(self.corners[1]-self.corners[0]) /
                               np.linalg.norm(self.anchor_corners[1]-self.anchor_corners[0]))
        scale = cumulative_scale/previous_scale
        predicted = cv2.transform(self.anchor_corners[None], transform)[0]
        shift = float(np.linalg.norm(predicted.mean(axis=0)-self.corners.mean(axis=0)))
        covered = cv2.contourArea(cv2.convexHull(a[use])) if use.sum() >= 3 else 0
        anchor_covered = cv2.contourArea(cv2.convexHull(original[use])) if use.sum() >= 3 else 0
        if (use.sum()<8 or use.mean()<.65 or spread[0]<w*.25 or spread[1]<h*.25
                or covered<w*h*.05 or anchor_spread[0]<aw*.25 or anchor_spread[1]<ah*.25
                or anchor_covered<aw*ah*.05 or not .90<=scale<=1.10 or shift>25
                or not .65<=cumulative_scale<=5.):
            return {'ok':False,'why':'incoherent or clustered feature motion',
                    'anchor_scale':cumulative_scale, 'inliers':int(use.sum())}
        self.points = b[use].reshape(-1,1,2)
        self.anchor_points = original[use].reshape(-1,1,2)
        self.corners = predicted
        lo, hi = predicted.min(axis=0), predicted.max(axis=0)
        box=[float(lo[0]),float(lo[1]),float(hi[0]-lo[0]),float(hi[1]-lo[1])]
        return {'ok':True,'bbox':box,'inliers':int(use.sum()),'shift_px':shift,
                'feature_scale':scale,'anchor_scale':cumulative_scale,
                'anchor_coverage':anchor_covered/(aw*ah),
                'center':list(map(float,predicted.mean(axis=0)))}
