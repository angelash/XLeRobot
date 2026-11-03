"""Real-time single-object tracker for the wrist camera.

Normalised cross-correlation template matching at half resolution, searched
around the predicted box over a few scales; the template adapts slowly while
confidence is high and the box is fully in view. No learned model: it follows
whatever the operator (or one large-model call) boxed. Frames are padded so a
box may leave the image partly - the object's near edge passes the bottom of
the wrist view when it enters the jaws.
"""
from __future__ import annotations

import cv2
import numpy as np


class TemplateTracker:
    def __init__(self, scales=(0.9, 0.95, 1.0, 1.05, 1.1), wide_scales=(0.75, 0.83, 1.2, 1.33), margin=40, min_score=0.45,
                 adapt=0.15, adapt_score=0.72, lost_after=5, downscale=0.5):
        self.scales, self.wide_scales = scales, wide_scales
        self.margin, self.min_score, self.adapt, self.adapt_score = margin, min_score, adapt, adapt_score
        self.lost_after, self.ds = lost_after, downscale
        self.reset()

    def reset(self):
        self.template = self.base = None
        self.box = None
        self.size0 = None
        self.scale = 1.0
        self.scale_rate = 1.0
        self.velocity = np.zeros(2)
        self.score = 0.0
        self.lost = 0
        self.frames = 0

    @property
    def active(self):
        return self.template is not None

    def _prep(self, gray):
        g = cv2.resize(gray, None, fx=self.ds, fy=self.ds, interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(g, (3, 3), 0).astype(np.float32)

    def init(self, gray, bbox):
        x, y, w, h = (float(v) for v in bbox)
        H, W = gray.shape[:2]
        if w < 16 or h < 16 or x < 0 or y < 0 or x + w > W or y + h > H:
            raise ValueError('目标框需完整在画面内且至少16像素')
        g = self._prep(gray)
        xs, ys, ws, hs = (int(round(v*self.ds)) for v in (x, y, w, h))
        patch = g[ys:ys + hs, xs:xs + ws]
        if patch.std() < 2.0:
            raise ValueError('目标框内几乎没有纹理，无法跟踪')
        self.base = patch.copy()
        self.template = patch.copy()
        self.size0 = (ws, hs)
        self.box = np.array([xs + ws/2, ys + hs/2], float)
        self.scale = 1.0
        self.scale_rate = 1.0
        self.velocity = np.zeros(2)
        self.score, self.lost, self.frames = 1.0, 0, 0
        return self.state()

    def _match(self, padded, pad, centre, scales, margin, base_scale, frame_h=None):
        """Best (score, centre, scale, w, h). The adaptive template locates; the
        initial template also votes on scale so a drifting template cannot
        shrink onto part of the object. When the box would run past the bottom
        of the frame (object entering the jaws) only the template rows that can
        still be in view are matched."""
        best = None
        H, W = padded.shape
        frame_h = frame_h or (H - 2*pad)
        for s in scales:
            sc = float(np.clip(base_scale*s, 0.2, 5.0))
            tw, th = int(round(self.size0[0]*sc)), int(round(self.size0[1]*sc))
            if tw < 8 or th < 8 or tw >= W - 2 or th >= H - 2:
                continue
            top = centre[1] - th/2
            keep = th if top + th <= frame_h - 2 else int(max(0.35*th, min(th, frame_h - 2 - top)))
            if margin is None:
                x0, y0, x1, y1 = 0, 0, W, H
            else:
                m = margin + 0.15*max(tw, th)
                x0 = int(max(0, centre[0] + pad - tw/2 - m)); y0 = int(max(0, centre[1] + pad - th/2 - m))
                x1 = int(min(W, centre[0] + pad + tw/2 + m)); y1 = int(min(H, centre[1] + pad - th/2 + keep + m))
            win = padded[y0:y1, x0:x1]
            if win.shape[0] < keep + 1 or win.shape[1] < tw + 1:
                continue
            tmpl = cv2.resize(self.template, (tw, th), interpolation=cv2.INTER_LINEAR)[:keep]
            base = cv2.resize(self.base, (tw, th), interpolation=cv2.INTER_LINEAR)[:keep]
            res = 0.6*cv2.matchTemplate(win, tmpl, cv2.TM_CCOEFF_NORMED) + 0.4*cv2.matchTemplate(win, base, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(res)
            mv -= 0.005*abs(np.log(s))         # slight preference for the predicted scale
            if best is None or mv > best[0]:
                best = (mv, np.array([x0 + ml[0] + tw/2 - pad, y0 + ml[1] + th/2 - pad]), sc, tw, th)
        return best

    def update(self, gray):
        if not self.active:
            return self.state()
        g = self._prep(gray)
        h0, w0 = g.shape
        predicted_scale = self.scale*self.scale_rate
        pad = int(max(self.size0)*predicted_scale*0.6) + 4
        padded = cv2.copyMakeBorder(g, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
        predicted = self.box + self.velocity
        best = self._match(padded, pad, predicted, self.scales, self.margin, predicted_scale)
        if best is not None and best[0] >= self.min_score:
            fine = self._match(padded, pad, best[1], (0.975, 1.0, 1.025), 6, best[2])
            if fine is not None and fine[0] >= best[0]:
                best = fine
        if best is None or best[0] < self.min_score:
            wide = self._match(padded, pad, predicted, self.scales + self.wide_scales,
                               None if self.lost >= self.lost_after else self.margin*2, self.scale)
            if wide is not None and (best is None or wide[0] > best[0]):
                best = wide
        self.frames += 1
        if best is None or best[0] < self.min_score:
            self.lost += 1
            self.score = 0.0 if best is None else float(best[0])
            self.velocity *= 0.5
            self.scale_rate = 1.0
            return self.state()
        score, centre, sc, tw, th = best
        self.velocity = 0.6*self.velocity + 0.4*(centre - self.box)
        self.scale_rate = float(np.clip(0.6*self.scale_rate + 0.4*(sc/self.scale), 0.9, 1.12))
        self.box, self.scale, self.score, self.lost = centre, sc, float(score), 0
        x0, y0 = int(round(centre[0] - tw/2)), int(round(centre[1] - th/2))
        if score >= self.adapt_score and x0 >= 0 and y0 >= 0 and x0 + tw <= w0 and y0 + th <= h0:
            patch = cv2.resize(g[y0:y0 + th, x0:x0 + tw], self.size0, interpolation=cv2.INTER_AREA)
            self.template = (1 - self.adapt)*self.template + self.adapt*patch
        return self.state()

    def state(self):
        if not self.active:
            return {'ok': False, 'active': False, 'score': 0.0, 'bbox': None, 'center': None, 'scale': None, 'lost': 0}
        tw, th = float(self.size0[0]*self.scale/self.ds), float(self.size0[1]*self.scale/self.ds)
        cx, cy = (float(v) for v in self.box/self.ds)
        return {'ok': self.lost == 0, 'active': True, 'score': round(float(self.score), 3), 'lost': int(self.lost),
                'bbox': [round(cx - tw/2, 1), round(cy - th/2, 1), round(tw, 1), round(th, 1)],
                'center': [round(cx, 1), round(cy, 1)], 'scale': round(float(self.scale), 3)}
