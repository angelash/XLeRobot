"""Recent frames from the existing MJPEG owner, without reopening a camera."""
from dataclasses import dataclass
import threading
import time


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    captured_at: float
    sequence: int
    monotonic_at: float


class CameraSnapshots:
    def __init__(self):
        self.lock = threading.Lock()
        self.frames = {}
        self.owners = set()

    def claim(self, camera):
        with self.lock:
            if camera in self.owners:
                return False
            self.owners.add(camera)
            self.frames.pop(camera, None)
            return True

    def release(self, camera):
        with self.lock:
            self.owners.discard(camera)
            self.frames.pop(camera, None)

    def publish(self, camera, jpeg, sequence):
        with self.lock:
            self.frames[camera] = Frame(jpeg, time.time(), sequence, time.monotonic())

    def latest(self, camera, max_age=2.):
        with self.lock:
            frame = self.frames.get(camera)
            if camera not in self.owners or frame is None:
                raise ValueError('相机尚无共享画面；请先打开该路实时画面')
            if time.monotonic() - frame.monotonic_at > max_age:
                raise ValueError('共享画面已过期，等待相机恢复后再观察')
            return frame
