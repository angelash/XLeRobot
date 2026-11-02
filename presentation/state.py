"""Validated presentation contracts and bounded local state. No hardware imports."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import struct
import threading
from typing import Literal
import uuid

from PIL import Image, ImageOps
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = ROOT.parent / 'fabrication/assembly/debug_console/platform-data/presence'
ASSET_ID = re.compile(r'^[a-f0-9]{32}\.(png|glb)$')
MAX_IMAGE = 10 * 1024**2
MAX_AVATAR = 20 * 1024**2


def now():
    return datetime.now(timezone.utc).isoformat()


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Avatar(Contract):
    kind: Literal['licensed-moss', 'procedural-moss', 'glb', 'image'] = 'licensed-moss'
    detail: Literal['detailed', 'simple'] = 'detailed'
    asset_id: str | None = Field(None, pattern=r'^[a-f0-9]{32}\.(png|glb)$')
    credit: str = Field('', max_length=500)

    @model_validator(mode='after')
    def check_asset(self):
        if self.kind in ('glb', 'image'):
            suffix = '.glb' if self.kind == 'glb' else '.png'
            if not self.asset_id or not self.asset_id.endswith(suffix):
                raise ValueError('形象类型与资源格式不一致')
        return self


class Layout(Contract):
    log_open: bool = True
    split: float = Field(.58, ge=.4, le=.7)
    animate: bool = True


class Profile(Contract):
    version: Literal[1] = 1
    name: str = Field('MOSS', min_length=1, max_length=40)
    subtitle: str = Field('550W · 机器人智能终端', max_length=100)
    greeting: str = Field('我在。你可以告诉我接下来要做什么。', max_length=240)
    persona: str = Field('冷静、简洁、准确，使用中文。尊重人的决定。明确区分已观察的事实、推断和未确认事项。保持 MOSS 式克制语气，不引用剧情中的欺骗、越权或伤害倾向。', max_length=4000)
    accent: str = Field('#ef493e', pattern=r'^#[0-9a-fA-F]{6}$')
    avatar: Avatar = Field(default_factory=Avatar)
    layout: Layout = Field(default_factory=Layout)


class Overlay(Contract):
    label: str = Field(min_length=1, max_length=80)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)
    confidence: float | None = Field(None, ge=0, le=1)

    @model_validator(mode='after')
    def check_bounds(self):
        if self.x + self.w > 1.000001 or self.y + self.h > 1.000001:
            raise ValueError('标注超出原图边界')
        return self


class Event(Contract):
    version: Literal[1] = 1
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=120)
    kind: Literal['system', 'task', 'summary', 'tool', 'vision', 'message']
    phase: Literal['started', 'progress', 'completed', 'failed', 'cancelled', 'interrupted'] = 'progress'
    title: str = Field(min_length=1, max_length=160)
    text: str = Field('', max_length=8000)
    source: str = Field('producer', min_length=1, max_length=80)
    evidence: Literal['reported', 'computed', 'demo'] = 'reported'
    task_id: str | None = Field(None, max_length=120)
    frame_id: str | None = Field(None, max_length=120)
    media_id: str | None = Field(None, pattern=r'^[a-f0-9]{32}\.png$')
    captured_at: AwareDatetime | None = None
    overlays: list[Overlay] = Field(default_factory=list, max_length=64)

    @model_validator(mode='after')
    def check_frame(self):
        if self.overlays and (not self.frame_id or not self.media_id):
            raise ValueError('标注必须绑定 frame_id 与 media_id')
        return self


class Journal:
    def __init__(self, limit=500):
        self.limit = limit
        self.epoch = uuid.uuid4().hex
        self.seq = 0
        self.events = OrderedDict()
        self.lock = threading.RLock()

    def add(self, event: Event):
        payload = event.model_dump(mode='json')
        with self.lock:
            existing = self.events.get(event.event_id)
            if existing:
                if any(existing.get(k) != v for k, v in payload.items()):
                    raise ValueError('event_id 已用于不同内容')
                return existing
            self.seq += 1
            value = {**payload, 'seq': self.seq, 'epoch': self.epoch, 'received_at': now()}
            self.events[event.event_id] = value
            while len(self.events) > self.limit:
                self.events.popitem(last=False)
            return value

    def read(self, after=0, epoch=None):
        with self.lock:
            values = list(self.events.values())
            oldest = values[0]['seq'] if values else self.seq + 1
            reset = (epoch is not None and epoch != self.epoch) or after > self.seq or (0 < after < oldest - 1)
            return {'epoch': self.epoch, 'seq': self.seq, 'reset': reset,
                    'events': values if reset else [x for x in values if x['seq'] > after]}


def validate_glb(data):
    if len(data) < 20 or len(data) > MAX_AVATAR:
        raise ValueError('GLB 文件长度无效或超过 20MiB')
    magic, version, length = struct.unpack_from('<4sII', data)
    if magic != b'glTF' or version != 2 or length != len(data):
        raise ValueError('只支持有效的 glTF 2.0 Binary 单文件')
    offset, document = 12, None
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError('GLB 数据块被截断')
        size, kind = struct.unpack_from('<II', data, offset)
        offset += 8
        if size % 4 or offset + size > len(data):
            raise ValueError('GLB 数据块长度无效')
        if document is None:
            if kind != 0x4E4F534A:
                raise ValueError('GLB 第一个块必须是 JSON')
            document = json.loads(data[offset:offset + size].decode('utf-8').rstrip(' \x00'))
        offset += size
    if not isinstance(document, dict) or not isinstance(document.get('asset'), dict) or document['asset'].get('version') != '2.0':
        raise ValueError('GLB 缺少 glTF 2.0 资产说明')
    # Check every URI, including extension objects, rather than only core images.
    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'uri' and (not isinstance(item, str) or not item.startswith('data:')):
                    raise ValueError('GLB 不能引用外部纹理、文件或网络地址')
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(document)
    if document.get('extensionsRequired'):
        raise ValueError('首版请导出不依赖压缩/外部解码扩展的 GLB')
    return document


class Store:
    def __init__(self, root=DEFAULT_DATA, quota=64 * 1024**2):
        self.root = Path(root)
        self.quota = quota
        self.lock = threading.RLock()
        self.journal = Journal()

    def profile(self):
        with self.lock:
            path = self.root / 'profile.json'
            return Profile.model_validate_json(path.read_text(encoding='utf-8')) if path.exists() else Profile()

    def save(self, profile):
        with self.lock:
            if profile.avatar.kind in ('glb', 'image'):
                self.asset(profile.avatar.asset_id)
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / 'profile.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_text(profile.model_dump_json(indent=2), encoding='utf-8')
            temporary.replace(path)
        return profile

    def asset(self, asset_id):
        if not ASSET_ID.fullmatch(asset_id or ''):
            raise ValueError('资源 ID 无效')
        path = self.root / 'media' / asset_id
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError('资源不存在，请重新导入')
        return path

    def put(self, data, is_avatar=False):
        limit = MAX_AVATAR if is_avatar else MAX_IMAGE
        if len(data) > limit:
            raise ValueError('上传文件超过大小限制')
        info = {}
        if is_avatar and data.startswith(b'glTF'):
            validate_glb(data)
            suffix, kind = '.glb', 'glb'
        else:
            try:
                with Image.open(BytesIO(data)) as image:
                    if image.format not in ('PNG', 'JPEG', 'WEBP') or image.width * image.height > 12_000_000:
                        raise ValueError('只支持不超过 1200 万像素的 PNG/JPEG/WebP')
                    image.load()
                    image = ImageOps.exif_transpose(image)
                    image = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')
                    image.info.clear()
                    info = {'width': image.width, 'height': image.height}
                    output = BytesIO()
                    image.save(output, format='PNG', exif=b'')
                    data = output.getvalue()
            except (OSError, Image.DecompressionBombError) as exc:
                raise ValueError('文件不是有效的图片或自包含 GLB') from exc
            suffix, kind = '.png', 'image'
        asset_id = uuid.uuid4().hex + suffix
        with self.lock:
            media = self.root / 'media'
            media.mkdir(parents=True, exist_ok=True)
            used = sum(p.stat().st_size for p in media.iterdir() if p.is_file())
            if used + len(data) > self.quota:
                raise ValueError('媒体库达到 64MiB 上限；请在任务结束后整理本地资源')
            (media / asset_id).write_bytes(data)
        return {'id': asset_id, 'kind': kind, 'url': '/api/presence/media/' + asset_id,
                'size': len(data), 'sha256': sha256(data).hexdigest(), **info}

    def publish(self, event):
        if event.media_id:
            self.asset(event.media_id)
        return self.journal.add(event)


def quality(path):
    """Pixel statistics, not object recognition or a semantic vision model."""
    import numpy as np
    with Image.open(path) as source:
        image = source.convert('L')
        image.thumbnail((1280, 960))
        pixels = np.asarray(image, dtype=np.float32)
        lap = -4 * pixels[1:-1, 1:-1] + pixels[1:-1, :-2] + pixels[1:-1, 2:] + pixels[:-2, 1:-1] + pixels[2:, 1:-1]
        return {'brightness': round(float(pixels.mean()), 1),
                'dark_fraction': round(float((pixels < 25).mean()), 4),
                'bright_fraction': round(float((pixels > 235).mean()), 4),
                'laplacian_variance': round(float(lap.var()), 1) if lap.size else 0,
                'width': source.width, 'height': source.height}
