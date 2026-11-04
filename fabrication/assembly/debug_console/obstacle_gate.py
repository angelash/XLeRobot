"""Freshness-checked obstacle gate inside the single serial owner.

The perception process (Studio) posts one message per processed frame. Motion
code asks the gate before and during forward driving; anything stale,
uncalibrated, invalid or too close blocks. Absence of messages always blocks.
"""
from __future__ import annotations

import math
import threading
import time

MAX_AGE_S = 0.35


class PerceptionGate:
    def __init__(self, clock=time.monotonic, max_age_s=MAX_AGE_S):
        self.clock = clock
        self.max_age_s = max_age_s
        self.lock = threading.Lock()
        self.latest = None
        self.received = None
        self.stream = None
        self.blocks = 0

    def update(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('感知消息需为对象')
        stream, seq = str(payload.get('stream', ''))[:40], payload.get('seq')
        if not stream or type(seq) is not int or seq < 0:
            raise ValueError('感知消息缺少stream或seq')
        clear = payload.get('clear_m')
        if clear is not None and (isinstance(clear, bool) or not isinstance(clear, (int, float)) or not math.isfinite(clear) or clear < 0):
            raise ValueError('clear_m无效')
        source_age = payload.get('frame_age_ms')
        if source_age is not None and (isinstance(source_age, bool) or not isinstance(source_age, (int, float))
                                       or not math.isfinite(source_age) or source_age < 0):
            raise ValueError('frame_age_ms无效')
        message = {'stream': stream, 'seq': seq, 'valid': payload.get('valid') is True, 'metric': payload.get('metric') is True,
                   'head_pose_ok': payload.get('head_pose_ok') is True, 'clear_m': None if clear is None else float(clear),
                   'blind_m': payload.get('blind_m'), 'processing_ms': payload.get('processing_ms'),
                   'frame_age_ms': payload.get('frame_age_ms'), 'reasons': [str(r)[:120] for r in (payload.get('reasons') or [])][:6],
                   'obstacles': payload.get('obstacles') if isinstance(payload.get('obstacles'), list) else []}
        with self.lock:
            if self.latest and stream == self.stream and seq <= self.latest['seq']:
                return {'accepted': False, 'reason': '序号未递增'}
            self.latest, self.received, self.stream = message, self.clock(), stream
        return {'accepted': True}

    def check(self, needed_m):
        """Return (ok, reason, message) for forward motion needing needed_m of clearance."""
        with self.lock:
            message, received = self.latest, self.received
        if not message:
            return False, '没有视觉避障数据', None
        age = self.clock()-received
        if age > self.max_age_s:
            reason = f'视觉避障数据过期{age*1000:.0f}ms'
        elif not message['valid']:
            reason = '视觉避障无效：'+('；'.join(message['reasons']) or '未说明')
        elif not message['metric']:
            reason = '视觉避障未完成地面标定，不能用于行驶'
        elif not message['head_pose_ok']:
            reason = '头部不在标定的导航姿态'
        elif message['frame_age_ms'] is None:
            reason = '视觉避障源画面时间未知'
        elif message['frame_age_ms'] + age*1000 > self.max_age_s*1000:
            reason = f"视觉避障源画面过期{message['frame_age_ms'] + age*1000:.0f}ms"
        elif message['clear_m'] is None or message['clear_m'] < needed_m:
            reason = f"前方净空{message['clear_m'] if message['clear_m'] is not None else '未知'}m，需要{needed_m:.2f}m"
        else:
            return True, None, message
        with self.lock:
            self.blocks += 1
        return False, reason, message

    def snapshot(self):
        with self.lock:
            message, received, blocks = self.latest, self.received, self.blocks
        if not message:
            return {'present': False, 'max_age_ms': round(self.max_age_s*1000), 'blocks': blocks}
        return {'present': True, **message, 'age_ms': round((self.clock()-received)*1000), 'max_age_ms': round(self.max_age_s*1000),
                'effective_frame_age_ms': None if message['frame_age_ms'] is None else round(message['frame_age_ms']+(self.clock()-received)*1000),
                'blocks': blocks}
