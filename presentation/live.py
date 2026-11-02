"""Bounded, coalesced public progress. Never stores PCM or raw engine events."""
from collections import OrderedDict
import re
import threading
import time
import uuid


class LiveFeed:
    def __init__(self, limit=64):
        self.epoch = uuid.uuid4().hex
        self.seq = 0
        self.values = OrderedDict()
        self.limit = limit
        self.lock = threading.RLock()

    def put(self, kind, value, key=None):
        with self.lock:
            self.seq += 1
            entry = dict(value, kind=kind, live_seq=self.seq, live_epoch=self.epoch)
            self.values[key or kind] = entry
            self.values.move_to_end(key or kind)
            while len(self.values) > self.limit:
                self.values.popitem(last=False)
            return entry

    def read(self, after=0, epoch=None):
        with self.lock:
            reset = epoch != self.epoch or after > self.seq
            return {'epoch': self.epoch, 'seq': self.seq, 'reset': reset,
                    'events': [v for v in self.values.values() if reset or v['live_seq'] > after]}


class PublicReplies:
    """Only allowlisted public prose enters this registry and sentence playback."""
    def __init__(self, feed):
        self.feed = feed
        self.values = OrderedDict()
        self.lock = threading.RLock()

    def update(self, request_id, text, *, status='running', turn_id=None):
        with self.lock:
            text = str(text)[:8000]
            old = self.values.get(request_id)
            if old and old['status'] != 'running':
                return old
            if old and old['text'] == text and old['status'] == status:
                return old
            generation = old['generation_id'] if old and text.startswith(old['text']) else uuid.uuid4().hex
            # Unfinished sentences/code blocks are held until stable or terminal.
            prose = re.sub(r'```.*?```', '', text, flags=re.S).split('```', 1)[0]
            stable = prose if status == 'completed' else prose[:max([m.end() for m in re.finditer(r'[。！？!?；;\n]', prose)] or [0])]
            from .speech import spoken_parts
            # Each finished sentence has immutable indices as later text grows.
            parts = [part for sentence in re.findall(r'[^。！？!?；;\n]*[。！？!?；;\n]|[^。！？!?；;\n]+$', stable)
                     for part in spoken_parts(sentence, limit=180)]
            value = {'request_id': request_id, 'text': text, 'status': status,
                     'turn_id': turn_id, 'generation_id': generation, 'parts': parts,
                     'updated_at': time.time()}
            self.values[request_id] = value
            self.values.move_to_end(request_id)
            while len(self.values) > 32:
                self.values.popitem(last=False)
            self.feed.put('reply.delta' if status == 'running' else 'reply.' + status,
                          {k: v for k, v in value.items() if k != 'parts'} | {'part_count': len(parts)}, 'reply-' + request_id)
            return value

    def get(self, request_id):
        with self.lock:
            return self.values.get(request_id)
