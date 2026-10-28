"""Guarded first-motion controller for this XLeRobot 0.4 dual-wheel unit.

No full-range reset and no hidden startup motion. LB is a dead-man switch.
Wheels stay off the floor for the first test. Ctrl+C, loss of the gamepad,
loss of focus/button release, or watchdog expiry stops motion.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame
from lerobot.motors import Motor, MotorCalibration, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus, OperatingMode
from runtime_state import write_state


ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "fabrication" / "assembly" / "evidence" / "2026-10-27"
STATE_DIR.mkdir(parents=True, exist_ok=True)
CALIBRATION_FILE = STATE_DIR / "first-motion-calibration.json"


def windows_ch343_ports() -> dict[str, str]:
    """Map CH343 serial numbers to current COM ports; no serial path is opened."""
    if sys.platform != "win32":
        return {}
    script = "Get-CimInstance Win32_SerialPort | Select-Object DeviceID,PNPDeviceID | ConvertTo-Json -Compress"
    process = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if not process.stdout.strip():
        return {}
    devices = json.loads(process.stdout)
    if isinstance(devices, dict):
        devices = [devices]
    result = {}
    for device in devices:
        match = __import__("re").search(r"VID_1A86&PID_55D3\\([0-9A-F]+)", device.get("PNPDeviceID", ""), __import__("re").I)
        if match:
            result[match.group(1).upper()] = device["DeviceID"]
    return result

JOINTS = {
    "left_arm_shoulder_pan": ("COM3", 1),
    "left_arm_shoulder_lift": ("COM3", 2),
    "left_arm_elbow_flex": ("COM3", 3),
    "left_arm_wrist_flex": ("COM3", 4),
    "left_arm_wrist_roll": ("COM3", 5),
    "left_arm_gripper": ("COM3", 6),
    "head_motor_1": ("COM3", 7),
    "head_motor_2": ("COM3", 8),
    "right_arm_shoulder_pan": ("COM4", 1),
    "right_arm_shoulder_lift": ("COM4", 2),
    "right_arm_elbow_flex": ("COM4", 3),
    "right_arm_wrist_flex": ("COM4", 4),
    "right_arm_wrist_roll": ("COM4", 5),
    "right_arm_gripper": ("COM4", 6),
}


def make_bus(port: str, names: list[str]) -> FeetechMotorsBus:
    motors = {}
    for name in names:
        _, motor_id = JOINTS[name]
        mode = MotorNormMode.RANGE_M100_100
        motors[name] = Motor(motor_id, "sts3215", mode)
    calibration = None
    if CALIBRATION_FILE.is_file():
        stored = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
        calibration = {
            name: MotorCalibration(**stored[name]) for name in names if name in stored
        }
    return FeetechMotorsBus(port=port, motors=motors, calibration=calibration)


def serials_present() -> list[str]:
    """Read Windows PnP state without opening a serial data path."""
    if sys.platform != "win32":
        return []
    script = "Get-PnpDevice -PresentOnly -Class Ports | Select-Object -ExpandProperty FriendlyName"
    process = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return [line.strip() for line in process.stdout.splitlines() if "CH343" in line]


class SafeMotion:
    def __init__(self, step: float, max_raw: int) -> None:
        self.left_names = [name for name in JOINTS if JOINTS[name][0] == "COM3" and name.startswith("left_arm")]
        self.head_names = [name for name in JOINTS if name.startswith("head")]
        self.right_names = [name for name in JOINTS if JOINTS[name][0] == "COM4"]
        port_map = windows_ch343_ports()
        # These serials were observed on this unit. Fallback only when both
        # adapters are absent from the map/OS setup; never guess a side after
        # only one device is present.
        left_port = port_map.get("5B61034756")
        right_port = port_map.get("5B61034338")
        if not left_port and not right_port and sys.platform != "win32":
            left_port, right_port = "COM3", "COM4"
        if not left_port or not right_port:
            raise RuntimeError(
                f"Need both CH343 serials 5B61034756 and 5B61034338; detected {port_map}. "
                "Do not connect/move with only one side present."
            )
        self.left_port, self.right_port = left_port, right_port
        self.bus3 = make_bus(left_port, self.left_names + self.head_names)
        self.bus4 = make_bus(right_port, self.right_names)
        self.buses = {3: self.bus3, 4: self.bus4}
        self.step = step
        self.max_raw = max_raw
        self.targets: dict[str, int] = {}
        self.active_torque = False

    def connect(self) -> None:
        # Empty calibration tells LeRobot to read existing motor limits on connect.
        self.bus3.connect(handshake=True)
        self.bus4.connect(handshake=True)
        for bus, names in ((self.bus3, self.left_names + self.head_names), (self.bus4, self.right_names)):
            # Guarantee torque-off before setting a position mode or reading.
            bus.disable_torque()
            # Read the calibration actually stored in the servos before use.
            if not bus.calibration:
                bus.calibration = {
                    name: MotorCalibration(
                        id=JOINTS[name][1], drive_mode=0, homing_offset=0, range_min=0, range_max=4095
                    )
                    for name in names
                }
            for name in names:
                bus.write("Operating_Mode", name, OperatingMode.POSITION.value, normalize=False)
                bus.write("P_Coefficient", name, 16, normalize=False)
                bus.write("I_Coefficient", name, 0, normalize=False)
                bus.write("D_Coefficient", name, 43, normalize=False)
        positions = self.read_positions()
        self.targets = {name: int(value) for name, value in positions.items()}

    def read_positions(self) -> dict[str, int]:
        out = {}
        out.update(self.bus3.sync_read("Present_Position", self.left_names + self.head_names, normalize=False))
        out.update(self.bus4.sync_read("Present_Position", self.right_names, normalize=False))
        return {name: int(value) for name, value in out.items()}

    def move(self, name: str, delta: int) -> None:
        if name not in self.targets:
            raise ValueError(f"Unknown joint {name}")
        target = max(0, min(4095, self.targets[name] + delta))
        if abs(target - self.targets[name]) > self.max_raw:
            raise ValueError("Per-step raw limit exceeded")
        bus = self.bus4 if JOINTS[name][0] == "COM4" else self.bus3
        bus.write("Goal_Position", name, target, normalize=False, num_retry=2)
        self.targets[name] = target

    def set_torque(self, enabled: bool) -> None:
        for bus in (self.bus3, self.bus4):
            if enabled:
                bus.enable_torque()
            else:
                bus.disable_torque()
        self.active_torque = enabled

    def shutdown(self) -> None:
        errors = []
        for bus in (self.bus3, self.bus4):
            try:
                bus.disable_torque()
            except Exception as exc:
                errors.append(str(exc))
            try:
                bus.disconnect(disable_torque=True)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            print("Shutdown warnings:", errors, file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", type=float, default=8.0, help="normalized units per update; default 8 of 100")
    parser.add_argument("--max-raw", type=int, default=50, help="raw-tick per-command safety cap")
    parser.add_argument("--fps", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.step <= 15 or not 1 <= args.max_raw <= 80:
        raise SystemExit("Refusing unsafe step. Keep step 1-15 and max-raw 1-80 for first motion.")

    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() < 1:
        raise SystemExit("No gamepad detected; press an Xbox button and retry.")
    pad = pygame.joystick.Joystick(0)
    pad.init()
    robot = SafeMotion(args.step, args.max_raw)
    print(f"[PORTS] left={robot.left_port}, right={robot.right_port}")
    write_state(
        "connecting", controller="online", torque=False, active_joint=None,
        selected_joint="left_arm_shoulder_pan", left_port=robot.left_port,
        right_port=robot.right_port, calibration=CALIBRATION_FILE.is_file(),
        last_action="控制器启动，扭矩关闭",
    )
    robot.connect()
    active = False
    joint_index = 0
    joint_names = list(JOINTS)
    previous_dpad = (0, 0)
    write_state(
        "ready", controller="online", torque=False, active_joint=None,
        selected_joint=joint_names[0], calibration=CALIBRATION_FILE.is_file(),
        last_action="等待按住 LB",
    )
    event_log = STATE_DIR / "first-motion-events.jsonl"
    running = True

    def stop_handler(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop_handler)
    print("[SAFE] Torque initially OFF. Hold LB to arm; release LB to stop.")
    print("[SAFE] First joint starts at LEFT SHOULDER PAN, not gripper.")
    print("[SAFE] Hold LB. D-pad Left/Right selects joint; D-pad Up/Down moves one step per fresh press.")
    print("[SAFE] Keep hands clear; Ctrl+C exits and disables torque.")
    try:
        last_heartbeat = 0.0
        while running:
            pygame.event.pump()
            axes = [pad.get_axis(i) for i in range(pad.get_numaxes())]
            buttons = [pad.get_button(i) for i in range(pad.get_numbuttons())]
            hats = pad.get_hat(0) if pad.get_numhats() else (0, 0)
            lb = bool(buttons[4]) if len(buttons) > 4 else False
            if lb != active:
                active = lb
                robot.set_torque(active)
                write_state(
                    "torque_on" if active else "torque_off", controller="online",
                    torque=active, active_joint=None, selected_joint=joint_names[joint_index],
                    last_action="LB 按住，可小步动作" if active else "LB 释放，动作已停",
                )
                with event_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"time": time.time(), "event": "lb", "enabled": active}) + "\n")
                print("[TORQUE]", "ENABLED - keep hand near release" if active else "DISABLED")
            dpad_left = hats[0] < 0 and previous_dpad[0] >= 0
            dpad_right = hats[0] > 0 and previous_dpad[0] <= 0
            dpad_up = hats[1] > 0 and previous_dpad[1] <= 0
            dpad_down = hats[1] < 0 and previous_dpad[1] >= 0
            previous_dpad = hats
            if (dpad_left or dpad_right) and active:
                joint_index = (joint_index + (1 if hats[0] > 0 else -1)) % len(joint_names)
                print("[JOINT]", joint_names[joint_index])
                write_state(
                    "joint_selected", controller="online", torque=active,
                    selected_joint=joint_names[joint_index], active_joint=None,
                    last_action=f"选中 {joint_names[joint_index]}",
                )
            if (dpad_up or dpad_down) and active:
                delta = int(args.step * 4096 / 200) * (1 if hats[1] > 0 else -1)
                try:
                    robot.move(joint_names[joint_index], delta)
                    write_state(
                        "step_sent", controller="online", torque=active,
                        selected_joint=joint_names[joint_index], active_joint=joint_names[joint_index],
                        delta_raw=delta, last_action=f"已发送 {joint_names[joint_index]} 一步",
                    )
                except Exception as exc:
                    print("[MOVE ERROR]", exc)
                    active = False
                    robot.set_torque(False)
                    write_state(
                        "motion_error", controller="online", torque=False,
                        selected_joint=joint_names[joint_index], active_joint=None,
                        error=str(exc), last_action="动作错误，已停止",
                    )
            if not lb and active:
                active = False
                robot.set_torque(False)
            if time.monotonic() - last_heartbeat >= 1.0:
                write_state(
                    "heartbeat", controller="online", torque=active,
                    selected_joint=joint_names[joint_index],
                    active_joint=joint_names[joint_index] if active and (dpad_up or dpad_down) else None,
                )
                last_heartbeat = time.monotonic()
            time.sleep(1 / args.fps)
    finally:
        robot.shutdown()
        pygame.quit()
        write_state(
            "shutdown", controller="offline", torque=False,
            active_joint=None, last_action="控制器退出，扭矩关闭", error=None,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
