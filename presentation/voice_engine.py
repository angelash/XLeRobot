"""Device-local KWS, neural VAD and streaming ASR; called only off the HTTP loop.

Audio lives in a bounded RAM pre-roll. This module never opens an audio device,
writes recordings, invokes an LLM or executes robot commands.
"""
from collections import deque
from hashlib import sha256
import json
from pathlib import Path
import re

import numpy as np


RATE = 16000
FRAME_SAMPLES = 320
FRAME_BYTES = 640


class VoiceError(RuntimeError):
    pass


def names_for(profile):
    names = [profile.name, *profile.voice_input.wake.aliases]
    if profile.name.upper() == 'MOSS':
        names.append('莫斯')
    return list(dict.fromkeys(x.strip() for x in names if x.strip()))


def strip_wake(text, names):
    # Strip only one confirmed leading invocation; retain names inside commands.
    for name in sorted(names, key=len, reverse=True):
        match = re.match(r'^\s*' + re.escape(name) + r'(?:[\s,，。:：!！?？]*)(.*)$', text, re.I)
        if match:
            return match[1].strip()
    return text.strip()


class LocalVoiceEngine:
    def __init__(self, root):
        self.root = Path(root) / 'voice-models'
        self.ready = False
        self.wake_ready = False
        self.unsupported = []
        self.wake_names = []
        self.profile = None
        self.state = 'off'

    def available(self):
        return (self.root / 'manifest.json').is_file()

    def _paths(self, section):
        result = {}
        for key, value in self.manifest[section].items():
            path = (self.root / value).resolve()
            if not path.is_relative_to(self.root.resolve()) or not path.is_file() or path.is_symlink():
                raise VoiceError('语音模型缺失，请重新准备本地模型')
            expected = self.manifest['sha256'].get(value)
            if not expected or sha256(path.read_bytes()).hexdigest() != expected:
                raise VoiceError('语音模型校验失败，请重新准备本地模型')
            result[key] = str(path)
        return result

    def prepare(self, profile):
        try:
            import sherpa_onnx
            from sherpa_onnx.utils import text2token
        except ImportError:
            raise VoiceError('本机缺少语音识别依赖，请安装 requirements-voice.txt') from None
        if not self.ready:
            if not self.available():
                raise VoiceError('本地语音模型尚未准备')
            self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
            kws, asr, vad = (self._paths(key) for key in ('kws', 'asr', 'vad'))
            self.kws_paths = kws
            # Default file is immutable and intentionally empty. Dynamic words
            # belong to each stream, so a renamed profile cannot reuse old words.
            self.kws = sherpa_onnx.KeywordSpotter(**{k: v for k, v in kws.items() if k != 'lexicon'},
                                                  num_threads=1, keywords_threshold=.25,
                                                  keywords_score=1.5, num_trailing_blanks=1)
            self.asr = sherpa_onnx.OnlineRecognizer.from_transducer(**asr, num_threads=2,
                        sample_rate=RATE, enable_endpoint_detection=True,
                        rule1_min_trailing_silence=2.4, rule2_min_trailing_silence=.5,
                        rule3_min_utterance_length=20, decoding_method='greedy_search')
            config = sherpa_onnx.VadModelConfig()
            config.silero_vad.model = vad['model']
            config.silero_vad.threshold = .5
            config.silero_vad.min_silence_duration = .1
            config.silero_vad.min_speech_duration = .1
            config.silero_vad.max_speech_duration = 25
            config.sample_rate = RATE
            config.num_threads = 1
            self.vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)
            self.ready = True
        names, unsupported, lines = [], [], []
        for name in names_for(profile):
            # Split mixed Chinese/English names and validate every generated token.
            words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[\u4e00-\u9fff]+", name)
            if ''.join(words).lower() != re.sub(r"[\s-]", '', name).lower():
                unsupported.append(name)
                continue
            try:
                tokens = text2token([' '.join(w.upper() if w.isascii() else w for w in words)],
                         tokens=self.kws_paths['tokens'], tokens_type='phone+ppinyin', lexicon=self.kws_paths['lexicon'])
                if not tokens or not tokens[0]:
                    raise ValueError()
                known = {line.split()[0] for line in Path(self.kws_paths['tokens']).read_text(encoding='utf-8').splitlines()}
                if not set(tokens[0]).issubset(known):
                    raise ValueError()
                # Result labels use safe stable indices, not keyword syntax.
                lines.append(' '.join(tokens[0]) + ' @wake_' + str(len(names)))
                names.append(name)
            except (ValueError, AssertionError, KeyError):
                unsupported.append(name)
        self.profile = profile.model_copy(deep=True)
        self.wake_names, self.unsupported = names, unsupported
        self.keywords = '\n'.join(lines)
        self.wake_ready = bool(lines)
        self.reset()
        return {'ready': True, 'wake_ready': self.wake_ready, 'wake_names': names, 'unsupported_names': unsupported}

    def reset(self, paused=False):
        self.pre_roll = deque(maxlen=75)
        self.vad_pending = np.empty(0, dtype=np.float32)
        self.vad.reset()
        self.kws_stream = self.kws.create_stream(self.keywords) if self.wake_ready else None
        self.asr_stream = None
        self.state = 'playback-paused' if paused else 'armed'
        self.total = 0
        self.last_speech = -RATE * 10
        self.last_wake = -RATE * 10
        self.started = None
        self.has_speech = False
        self.invoked = False
        self.partial = ''

    def begin(self, invoked=False):
        self.asr_stream = self.asr.create_stream()
        self.state = 'listening'
        self.started = self.total
        self.has_speech = False
        self.invoked = invoked
        self.partial = ''
        # Only wake captures need pre-roll. PTT begins exactly on the gesture.
        if invoked:
            for samples in self.pre_roll:
                self.asr_stream.accept_waveform(RATE, samples)
            self._decode()
            self.has_speech = bool(strip_wake(self.asr.get_result(self.asr_stream).strip(), self.wake_names))

    def _decode(self):
        while self.asr_stream and self.asr.is_ready(self.asr_stream):
            self.asr.decode_stream(self.asr_stream)

    def control(self, command):
        if command == 'ack-pause':
            self.before_ack = self.state
            self.state = 'playback-paused'
            self.pre_roll.clear()
        elif command == 'ack-resume':
            self.state = getattr(self, 'before_ack', 'armed')
            self.vad.reset()
            self.vad_pending = np.empty(0, dtype=np.float32)
            self.last_speech = self.total
        elif command == 'pause':
            self.reset(paused=True)
        elif command == 'resume':
            self.reset()
        elif command == 'begin' and self.state == 'armed':
            self.begin()
        elif command == 'end' and self.state == 'listening':
            return self.finish()
        elif command == 'cancel':
            self.reset()
        return []

    def finish(self):
        if not self.asr_stream:
            return []
        self.asr_stream.accept_waveform(RATE, np.zeros(8000, dtype=np.float32))
        self.asr_stream.input_finished()
        self._decode()
        text = self.asr.get_result(self.asr_stream).strip()
        if self.invoked:
            text = strip_wake(text, self.wake_names)
        useful = self.has_speech and bool(text)
        self.asr_stream = None
        self.state = 'armed'
        self.pre_roll.clear()
        self.kws_stream = self.kws.create_stream(self.keywords) if self.wake_ready else None
        self.last_wake = self.total
        self.vad.reset()
        return [{'kind': 'asr.final', 'text': text, 'quality': 'uncalibrated', 'requires_confirmation': True}] if useful else [{'kind': 'voice.empty', 'text': '没有识别到指令，请重说'}]

    def feed(self, pcm):
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        self.total += len(samples)
        if self.state == 'playback-paused':
            return []
        events = []
        self.vad_pending = np.concatenate((self.vad_pending, samples))
        while len(self.vad_pending) >= 512:
            self.vad.accept_waveform(self.vad_pending[:512])
            self.vad_pending = self.vad_pending[512:]
        # Consume completed VAD segments so its memory also stays bounded.
        speech = self.vad.is_speech_detected()
        while not self.vad.empty():
            self.vad.pop()
        if speech:
            self.last_speech = self.total
        self.pre_roll.append(samples.copy())
        if self.state == 'armed':
            if self.kws_stream and self.profile.voice_input.mode == 'wake-and-talk':
                self.kws_stream.accept_waveform(RATE, samples)
                while self.kws.is_ready(self.kws_stream):
                    self.kws.decode_stream(self.kws_stream)
                result = self.kws.get_result(self.kws_stream)
                cooldown = self.profile.voice_input.wake.cooldown_ms * RATE // 1000
                if result and self.total - self.last_speech < RATE and self.total - self.last_wake >= cooldown:
                    self.kws.reset_stream(self.kws_stream)
                    self.last_wake = self.total
                    self.begin(invoked=True)
                    events.append({'kind': 'voice.wake', 'text': '我在听', 'name': self.profile.name})
            return events
        if self.state != 'listening':
            return []
        self.asr_stream.accept_waveform(RATE, samples)
        self._decode()
        self.has_speech = self.has_speech or speech
        text = self.asr.get_result(self.asr_stream).strip()
        if text != self.partial:
            self.partial = text
            events.append({'kind': 'asr.partial', 'text': text})
        config = self.profile.voice_input.utterance
        duration = (self.total - self.started) * 1000 / RATE
        silence = (self.total - self.last_speech) * 1000 / RATE
        if duration >= config.max_ms:
            self.reset()
            return [{'kind': 'voice.empty', 'text': '单条语音超过20秒，请分句重说'}]
        if not self.has_speech and duration > config.start_timeout_ms:
            self.reset()
            return [{'kind': 'voice.empty', 'text': '没有听到指令，已回到待命'}]
        if self.has_speech and duration > 300 and silence >= config.endpoint_silence_ms:
            events.extend(self.finish())
        return events
