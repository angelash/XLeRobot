"""XLeRobot hardware console with a separate guarded first-motion controller.

The monitoring server queries buses read-only. Motor writes are only possible
inside the separately launched first-motion controller with LB held.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import serial
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

try:
    from fabrication.assembly.runtime_state import read_state
    from fabrication.assembly.debug_console.torque_control import TorqueSession
except ImportError:
    from runtime_state import read_state
    from torque_control import TorqueSession


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
EVIDENCE = ROOT.parent / "evidence" / "2026-10-27"
PORTS = ("COM3", "COM4")
BAUD = 1_000_000
MOTOR_IDS = range(1, 21)
STS3215_MODEL = 777
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")


CONTROLLER_PROCESS: subprocess.Popen | None = None
TORQUE = TorqueSession()
TORQUE_STATE: dict[str, Any] = {"enabled": False, "scope": None, "message": "扭矩关闭", "updated_at": None}


def gamepad_inventory() -> list[str]:
    """Enumerate OS-visible gamepads without opening the robot controller."""
    try:
        import pygame
        pygame.init()
        pygame.joystick.init()
        names = []
        for index in range(pygame.joystick.get_count()):
            joystick = pygame.joystick.Joystick(index)
            joystick.init()
            names.append(joystick.get_name())
        return names
    except Exception:
        return []


def checksum(body: list[int]) -> int:
    return (~sum(body)) & 0xFF


def packet(motor_id: int, instruction: int, params: list[int] | None = None) -> bytes:
    """Build only PING(1) or READ(2); any other instruction is rejected."""
    if instruction not in (1, 2):
        raise ValueError("read-only monitor permits only PING and READ")
    if not 1 <= motor_id <= 253:
        raise ValueError("broadcast IDs are forbidden")
    body = [motor_id, len(params or []) + 2, instruction, *(params or [])]
    return bytes([0xFF, 0xFF, *body, checksum(body)])


def query(port: serial.Serial, motor_id: int, instruction: int, params: list[int] | None = None) -> dict[str, Any] | None:
    port.reset_input_buffer()
    port.write(packet(motor_id, instruction, params))
    deadline = time.monotonic() + 0.10
    buffer = bytearray()
    while time.monotonic() < deadline:
        waiting = port.in_waiting
        if waiting:
            buffer.extend(port.read(waiting))
        for start in range(max(0, len(buffer) - 300), len(buffer) - 5):
            if buffer[start:start + 2] != b"\xff\xff" or buffer[start + 2] != motor_id:
                continue
            length = buffer[start + 3]
            # Frame: FF FF ID LEN ERR DATA... CHECKSUM. LEN counts ERR + DATA.
            end = start + length + 4
            if end > len(buffer):
                continue
            frame = buffer[start:end]
            if sum(frame[2:]) & 0xFF != 0xFF:
                continue
            data = list(frame[5:-1])
            return {"error": frame[4], "data": data}
        time.sleep(0.002)
    return None


def read_port(port_name: str) -> dict[str, Any]:
    result: dict[str, Any] = {"port": port_name, "connected": False, "motors": [], "error": None}
    port = serial.Serial()
    port.port = port_name
    port.baudrate = BAUD
    port.timeout = 0.10
    port.write_timeout = 0.10
    port.dtr = False
    port.rts = False
    try:
        port.open()
        result["connected"] = True
        # PING only. Existing IDs are retained; no motor is configured.
        responding: list[int] = []
        for motor_id in MOTOR_IDS:
            reply = query(port, motor_id, 1)
            if reply is not None and reply["error"] == 0:
                responding.append(motor_id)
            time.sleep(0.006)
        for motor_id in responding:
            reply = query(port, motor_id, 2, [0, 70])
            time.sleep(0.010)
            if reply is None or len(reply["data"]) != 70:
                result["motors"].append({"id": motor_id, "error": "register read failed"})
                continue
            reg = reply["data"]
            model = reg[3] + 256 * reg[4]
            result["motors"].append({
                "id": motor_id,
                "model": model,
                "model_name": "STS3215" if model == STS3215_MODEL else f"unknown-{model}",
                "torque": reg[40],
                "mode": reg[33],
                "position": reg[56] + 256 * reg[57],
                "speed": reg[58] + 256 * reg[59],
                "voltage": reg[62] / 10.0,
                "temperature": reg[63],
                "error": reply["error"],
            })
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        if port.is_open:
            port.close()
    result["motor_count"] = len(result["motors"])
    return result


class HardwareMonitor:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.report: dict[str, Any] = {
            "mode": "READ_ONLY",
            "updated_at": None,
            "ports": [],
            "cameras": [],
            "gamepad": {"status": "Browser Gamepad API; press a button to wake"},
            "controller": read_state(),
        }
        self.camera_health = [
            {"camera": 0, "name": "USB2.0_CAM1 · 01", "state": "等待首帧", "frames": 0},
            {"camera": 1, "name": "USB2.0_CAM1 · 02", "state": "等待首帧", "frames": 0},
            {"camera": 2, "name": "USB2.0_CAM1 · 03", "state": "等待首帧", "frames": 0},
        ]
        self.started = False
        self.paused = False
        self.serial_open = False

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        threading.Thread(target=self._motor_loop, daemon=True, name="motor-readonly").start()

    def _motor_loop(self) -> None:
        while True:
            if not self.paused:
                self.serial_open = True
                ports = []
                try:
                    for port in PORTS:
                        ports.append(read_port(port))
                finally:
                    self.serial_open = False
            with self.lock:
                if self.paused:
                    continue
                self.report["ports"] = ports
                self.report["cameras"] = [dict(item) for item in self.camera_health]
                pads = gamepad_inventory()
                self.report["gamepad"] = {"status": "OS_ENUMERATED", "devices": pads}
                state = read_state()
                if self.paused and CONTROLLER_PROCESS is None:
                    state = {**state, "controller": "offline", "torque": False}
                self.report["controller"] = state
                self.report["updated_at"] = dt.datetime.now().astimezone().isoformat()
            # Leave buses free and reduce traffic; no command/state writes occur.
            time.sleep(2.0)

    def set_camera(self, camera: int, state: str, frames: int | None = None) -> None:
        with self.lock:
            self.camera_health[camera]["state"] = state
            if frames is not None:
                self.camera_health[camera]["frames"] = frames
            self.report["cameras"] = [dict(item) for item in self.camera_health]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.report))


monitor = HardwareMonitor()
app = FastAPI(title="XLeRobot Read-only Debug Console")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.on_event("startup")
def start_monitor() -> None:
    monitor.start()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status() -> dict[str, Any]:
    snapshot = monitor.snapshot()
    snapshot["torque_control"] = dict(TORQUE_STATE)
    snapshot["controller_launch"] = {
        "can_start": True,
        "reason": "UI also requires 2 boards, 16 motors, 3 cameras, gamepad, calibration and explicit safety confirmation",
        "running": bool(CONTROLLER_PROCESS and CONTROLLER_PROCESS.poll() is None),
    }
    return snapshot


def _torque_selection(scope: str, port_name: str | None, motor_id: int | None) -> dict[str, list[int]] | None:
    if scope == "all":
        return None
    if scope != "single" or port_name not in PORTS or motor_id is None:
        raise HTTPException(status_code=400, detail="精细模式需要指定 COM3/COM4 和电机 ID")
    return {port_name: [int(motor_id)]}


@app.post("/api/torque")
def set_torque(payload: dict[str, Any]) -> dict[str, Any]:
    """Enable or disable holding torque without commanding a motor move."""
    global TORQUE_STATE
    if _controller_process():
        raise HTTPException(status_code=409, detail="受限控制器正在占用串口，先停止它")
    enabled = bool(payload.get("enabled"))
    scope = str(payload.get("scope", "all"))
    port_name = payload.get("port")
    motor_id = payload.get("id")
    selection = _torque_selection(scope, port_name, motor_id)
    monitor.paused = True
    deadline = time.monotonic() + 4.0
    while monitor.serial_open and time.monotonic() < deadline:
        time.sleep(0.05)
    if monitor.serial_open:
        monitor.paused = False
        raise HTTPException(status_code=503, detail="只读监控仍未释放串口，扭矩命令未发送")
    time.sleep(0.8)
    try:
        report = TORQUE.apply(enabled, selection)
    except Exception as exc:
        TORQUE.close()
        monitor.paused = False
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    TORQUE.close()
    monitor.paused = False
    mismatches = [item for item in report["motors"] if bool(item["torque"]) != enabled]
    TORQUE_STATE = {
        "enabled": enabled and not mismatches,
        "scope": scope,
        "count": report["count"],
        "message": ("已打开" if enabled else "已关闭") + f" {report['count']} 个舵机扭矩",
        "updated_at": dt.datetime.now().astimezone().isoformat(),
    }
    return {"status": "ok", **TORQUE_STATE, "report": report}


def _controller_process() -> subprocess.Popen | None:
    global CONTROLLER_PROCESS
    if CONTROLLER_PROCESS is not None and CONTROLLER_PROCESS.poll() is not None:
        CONTROLLER_PROCESS = None
    return CONTROLLER_PROCESS


@app.post("/api/controller/start")
def start_controller() -> dict[str, Any]:
    """Start the separately guarded controller; never send a motor command here."""
    global CONTROLLER_PROCESS
    current = monitor.snapshot()
    # The monitor needs an active serial bus for the safety gate. Pause it
    # before handing both COM ports to the separate controller.
    with monitor.lock:
        monitor.paused = True
    online = [p for p in current.get("ports", []) if p.get("connected") and not p.get("error")]
    motors = sum(p.get("motor_count", 0) for p in online)
    cameras = sum(c.get("state") == "在线" for c in current.get("cameras", []))
    pads = current.get("gamepad", {}).get("devices", [])
    calibration = (ROOT / "evidence" / "2026-10-27" / "first-motion-calibration.json").is_file()
    blockers = []
    if len(online) != 2:
        blockers.append(f"控制板 {len(online)}/2")
    if motors != 16:
        blockers.append(f"电机 {motors}/16")
    if cameras != 3:
        blockers.append(f"相机 {cameras}/3")
    if not pads:
        blockers.append("未连接手柄")
    if not calibration:
        blockers.append("未完成校准")
    if blockers:
        with monitor.lock:
            monitor.paused = False
        return {"status": "blocked", "message": "未启动：" + "；".join(blockers)}
    if _controller_process():
        with monitor.lock:
            monitor.paused = False
        return {"status": "running", "message": "受限控制器已经在运行；先停止再重启。"}
    python = ROOT.parents[2] / ".venv-robot" / "Scripts" / "python.exe"
    if not python.is_file():
        with monitor.lock:
            monitor.paused = False
        return {"status": "blocked", "message": "缺少 .venv-robot 硬件环境。"}
    CONTROLLER_PROCESS = subprocess.Popen(
        [str(python), "-X", "utf8", "fabrication/assembly/first_motion_controller.py", "--step", "8", "--max-raw", "50"],
        cwd=str(ROOT.parents[2]),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
    )
    return {"status": "starting", "message": "受限控制器已启动。控制器会先保持扭矩 OFF，等待手柄按 LB。"}


@app.post("/api/controller/stop")
def stop_controller() -> dict[str, Any]:
    process = _controller_process()
    if not process:
        with monitor.lock:
            monitor.paused = False
        return {"status": "stopped", "message": "受限控制器没有在运行。"}
    process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
    with monitor.lock:
        monitor.paused = False
    return {"status": "stopped", "message": "已请求停止；控制器正常退出时会关闭扭矩并释放串口。"}


@app.get("/api/evidence")
def evidence() -> dict[str, Any]:
    """Show the last confirmed read-only check so outages remain distinguishable."""
    files = {}
    for name in ("motor-summary.json", "camera-streams.json", "pnp-inventory.json"):
        path = EVIDENCE / name
        files[name] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    return {"saved_at": "2026-10-27", "files": files}


def mjpeg(camera: int):
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "dshow", "-video_device_number", str(camera),
        "-video_size", "640x480", "-framerate", "15",
        "-i", "video=USB2.0_CAM1", "-an", "-c:v", "mjpeg", "-q:v", "5",
        "-f", "image2pipe", "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    buffer = bytearray()
    frames = 0
    monitor.set_camera(camera, "连接中")
    try:
        while True:
            chunk = proc.stdout.read(8192) if proc.stdout else b""
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                end = buffer.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                if start < 0 or end < 0:
                    if len(buffer) > 2_000_000:
                        buffer.clear()
                    break
                jpeg = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                frames += 1
                monitor.set_camera(camera, "在线", frames)
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        monitor.set_camera(camera, "离线", frames)


@app.get("/camera/{camera}.mjpg")
def camera(camera: int):
    if camera not in (0, 1, 2):
        return {"error": "camera must be 0, 1 or 2"}
    return StreamingResponse(mjpeg(camera), media_type="multipart/x-mixed-replace; boundary=frame")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
