"""Find a known object again from the views kept in the grasp knowledge (MANIPULATION_V2_ZH.md R6).

OWL-ViT image-guided detection: the stored template is the query, the current
wrist frame the target. It runs on the workstation compute node (the laptop
has too little memory). The decision uses the raw box confidence after
dropping whole-frame boxes and boxes centred on the open jaws: on 2026-11-03
wrist frames the plug scored 0.67-0.85 (upright and lying, 0.6x scale change)
and frames without it at most 0.25, so 0.5 leaves a margin on both sides.
"""
from __future__ import annotations

import base64
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .knowledge import JAW_ZONES, iou

# 2026-11-03 18:0x: the same lying plug scored 0.85 / 0.64 / 0.46 on frames minutes apart, frames without it
# at most 0.25. One frame is not enough: up to three frames, any at >= 0.4, all best boxes in one place.
MIN_SCORE = 0.4
AGREE_IOU = 0.5
MAX_QUERIES = 3
MAX_FRAMES = 3


def decode_image(b64):
    data = np.frombuffer(base64.b64decode(b64), np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError('图像无法解码')
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def in_jaws(box):
    cx, cy = box[0] + box[2]/2, box[1] + box[3]/2
    return any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in JAW_ZONES)


def usable_box(box, width=640, height=480):
    """Survey targets must be complete and must not include both gripper jaws.

    A centre-only jaw exclusion accepted a 556x175 box spanning both jaws on
    2026-11-04. Temporal agreement does not remove that systematic false hit.
    """
    if box is None or len(box) != 4 or not np.isfinite(box).all():
        return False
    x, y, w, h = box
    if w < 20 or h < 20 or x < 4 or y < 4 or x + w > width - 4 or y + h > height - 4:
        return False
    if w*h > 0.35*width*height or in_jaws(box):
        return False
    overlaps = [max(0, min(x+w, x1)-max(x, x0))*max(0, min(y+h, y1)-max(y, y0))/(w*h)
                for x0, y0, x1, y1 in JAW_ZONES]
    return not all(overlap > 0.1 for overlap in overlaps)


class ExampleFinder:
    def __init__(self, model_dir):
        self.model_dir = Path(model_dir)
        self.lock = threading.Lock()
        self.model = self.proc = None
        self.load_s = None

    def available(self):
        return (self.model_dir/'config.json').is_file() and (self.model_dir/'model.safetensors').is_file()

    def _load(self):
        if self.model is None:
            started = time.perf_counter()
            from transformers import OwlViTForObjectDetection, OwlViTProcessor
            self.proc = OwlViTProcessor.from_pretrained(str(self.model_dir), local_files_only=True)
            self.model = OwlViTForObjectDetection.from_pretrained(str(self.model_dir), local_files_only=True).eval()
            self.load_s = round(time.perf_counter() - started, 2)
        return self.model, self.proc

    def candidates(self, image, query, keep=3):
        """Raw-confidence boxes [x, y, w, h] in image pixels, whole-frame and jaw boxes removed."""
        import torch
        from PIL import Image
        model, proc = self._load()
        h, w = image.shape[:2]
        with torch.no_grad():
            inputs = proc(images=Image.fromarray(image), query_images=Image.fromarray(query), return_tensors='pt')
            out = model.image_guided_detection(**inputs)
        raw = torch.sigmoid(out.logits[0, :, 0]).numpy()
        boxes = out.target_pred_boxes[0].numpy()
        rows = []
        for i in np.argsort(-raw):
            cx, cy, bw, bh = boxes[i]
            box = [float((cx - bw/2)*w), float((cy - bh/2)*h), float(bw*w), float(bh*h)]
            if not usable_box(box, w, h):
                continue
            rows.append({'score': round(float(raw[i]), 4), 'bbox': [round(v, 1) for v in box]})
            if len(rows) >= keep:
                break
        return rows

    def find(self, image, queries, min_score=MIN_SCORE):
        started = time.perf_counter()
        with self.lock:
            best, per_query = None, []
            for k, q in enumerate(queries[:MAX_QUERIES]):
                rows = self.candidates(image, q)
                per_query.append(rows[:2])
                if rows and (best is None or rows[0]['score'] > best['score']):
                    best = {**rows[0], 'query': k}
        result = {'ok': bool(best and best['score'] >= min_score), 'score': best and best['score'],
                  'bbox': best and best['bbox'], 'query': best and best['query'], 'per_query': per_query,
                  'min_score': min_score, 'model': self.model_dir.name, 'load_s': self.load_s,
                  'ms': round((time.perf_counter() - started)*1000, 1)}
        return result

    def run_payload(self, payload):
        frames = [decode_image(f) for f in (payload.get('images_b64') or [payload['image_b64']])[:MAX_FRAMES]]
        queries = [decode_image(q) for q in (payload.get('queries_b64') or [])[:MAX_QUERIES]]
        if not queries:
            raise ValueError('至少需要一张外观模板')
        min_score = float(payload.get('min_score', MIN_SCORE))
        results = [self.find(image, queries, min_score) for image in frames]
        best = max(results, key=lambda r: r['score'] or -1)
        boxes = [r['bbox'] for r in results if r['bbox']]
        agree = bool(best['bbox']) and len(boxes) == len(results) and all(iou(b, best['bbox']) >= AGREE_IOU for b in boxes)
        return {**best, 'ok': bool(best['score'] is not None and best['score'] >= min_score and agree), 'frames': len(results),
                'frame_scores': [r['score'] for r in results], 'agree': agree,
                'ms': round(sum(r['ms'] for r in results), 1)}

