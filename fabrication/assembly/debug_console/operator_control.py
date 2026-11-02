"""Native Menu lifecycle and a bounded, read-only operator event channel."""
from copy import deepcopy
from datetime import datetime, timezone
import threading
import time
import uuid

from .upstream_teleop import neutral


class LinkedModeUnavailable(ValueError):
    """Only configuration/IK limitations eligible for preset fallback."""


class OperatorEvents:
    def __init__(self):
        self.condition = threading.Condition()
        self.epoch = uuid.uuid4().hex
        self.revision = 0
        self.value = None
        self.changed_at = None

    def publish(self, value):
        with self.condition:
            if value == self.value:
                return
            self.value = deepcopy(value)
            self.revision += 1
            self.changed_at = datetime.now(timezone.utc).isoformat()
            self.condition.notify_all()

    def read(self):
        with self.condition:
            return {**deepcopy(self.value or {}), 'version': 1, 'epoch': self.epoch,
                    'seq': self.revision, 'changed_at': self.changed_at}

    def wait(self, revision, timeout=2):
        with self.condition:
            self.condition.wait_for(lambda: self.revision != revision, timeout)
            return self.read()


def _pressed(raw, index):
    return len(raw.get('buttons', [])) > index and bool(raw['buttons'][index])


def _centered(raw, allow_menu=False):
    value = {**raw, 'buttons': list(raw.get('buttons', []))}
    if allow_menu and len(value['buttons']) > 7:
        value['buttons'][7] = False
    return neutral(value)


class NativeMenuControl:
    def __init__(self, runtime, spawn=None):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.spawn = spawn or (lambda job: threading.Thread(target=job, daemon=True, name='menu-takeover').start())
        self.last_menu = None
        self.released = False
        self.pending = False
        self.busy = None
        self.cancelled = False
        self.error = None
        self.fallback = None
        self.owns_session = False

    def status(self):
        with self.lock:
            return {'pending': self.pending, 'busy': self.busy, 'error': self.error,
                    'fallback_reason': self.fallback, 'native_session': self.owns_session}

    def cancel(self, reason):
        with self.lock:
            self.pending = False
            self.cancelled = True
            self.error = reason

    def sample(self, raw):
        """No serial lock here: preparation must not block XInput sampling."""
        r = self.runtime
        with self.lock:
            menu = _pressed(raw, 7)
            if not raw.get('connected'):
                self.last_menu = None
                self.released = False
                self.pending = False
                self.cancelled = True
                if self.busy == 'starting':
                    self.error = '手柄断开，启动已取消'
                elif not self.busy and r.controller['controller'] == 'online':
                    self._launch('stopping', '手柄断开，已结束接管；重新连接后按Menu开始')
                return True
            edge = self.released and menu and self.last_menu is False
            self.last_menu = menu
            if not menu:
                self.released = True
            if _pressed(raw, 6):
                if self.busy == 'starting' or self.pending:
                    self.cancel('View已取消启动')
                elif not self.busy and r.controller['controller'] == 'online':
                    self._launch('stopping', 'View结束接管，保持当前位置')
                return True
            if self.busy:
                if self.busy == 'starting' and (edge or not _centered(raw, allow_menu=True)):
                    self.cancel('Menu已取消启动' if edge else '输入偏离回中，启动已取消')
                return True
            if edge:
                if r.controller['controller'] == 'online':
                    self._launch('stopping', 'Menu结束接管，保持当前位置，不归零')
                elif not _centered(raw, allow_menu=True):
                    self.error = '请松开其余按键并将摇杆、扳机回中，再按Menu'
                    self.pending = False
                else:
                    self.error = None
                    self.fallback = None
                    self.cancelled = False
                    self.pending = True
            if self.pending:
                if not _centered(raw, allow_menu=True):
                    self.cancel('输入偏离回中，请回中后重新按Menu')
                elif not menu:
                    self.pending = False
                    self._launch('starting')
                return True
            return menu

    def _launch(self, action, reason=None):
        self.busy = action
        if action == 'starting':
            self.cancelled = False
        self.spawn(lambda: self._work(action, reason))

    def _check_start(self):
        r = self.runtime
        with self.lock:
            if self.cancelled or r.stop_event.is_set():
                raise ValueError(self.error or '启动已取消')
        if time.monotonic()-r.pad_time > .5 or not _centered(r.gamepad):
            raise ValueError('手柄输入过期或未回中，请重新按Menu')

    def _work(self, action, reason):
        r = self.runtime
        failure = None
        try:
            with r.io:
                if action == 'stopping':
                    r._stop(reason)
                    self.owns_session = False
                else:
                    self._check_start()
                    r._require_not_calibrating()
                    if getattr(getattr(r, 'agent', None), 'running', False):
                        raise ValueError('助手任务正在运行或收尾，请结束后重新按Menu')
                    if getattr(r, 'manual_pose_active', False):
                        raise ValueError('手动摆位尚未结束，请先锁定或结束摆位')
                    try:
                        r.begin_whole_robot(True)
                    except LinkedModeUnavailable as exc:
                        self._check_start()
                        with self.lock:
                            self.fallback = str(exc)[:1200]
                        r.publish_operator_state()
                        r.begin_preset(True)
                    self._check_start()
                    self.owns_session = True
        except Exception as exc:
            failure = str(exc)
            with r.io:
                if action == 'starting' and r.controller['controller'] == 'online':
                    r._stop('启动取消或失败，保持当前位置：'+failure)
            self.owns_session = False
        finally:
            with self.lock:
                self.busy = None
                self.pending = False
                if failure:
                    self.error = failure
                elif action == 'stopping':
                    self.error = None
            r.log('operator_menu', failure or reason or 'Menu接管准备完成')
            r.publish_operator_state(refresh_setup=True)
