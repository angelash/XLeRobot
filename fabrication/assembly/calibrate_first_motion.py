"""Manual, torque-off calibration for the first guarded Xbox test.

Calibration values are written both to the motors and to the local JSON cache.
The script does not move the robot; you guide each arm through its real range.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus, OperatingMode

STATE = Path(__file__).resolve().parent / "evidence" / "2026-10-27"
CALIBRATION_FILE = STATE / "first-motion-calibration.json"
JOINTS = {
    "COM3": {
        "left_arm_shoulder_pan": 1,
        "left_arm_shoulder_lift": 2,
        "left_arm_elbow_flex": 3,
        "left_arm_wrist_flex": 4,
        "left_arm_wrist_roll": 5,
        "left_arm_gripper": 6,
        "head_motor_1": 7,
        "head_motor_2": 8,
    },
    "COM4": {
        "right_arm_shoulder_pan": 1,
        "right_arm_shoulder_lift": 2,
        "right_arm_elbow_flex": 3,
        "right_arm_wrist_flex": 4,
        "right_arm_wrist_roll": 5,
        "right_arm_gripper": 6,
    },
}


def calibrate_port(port: str, motors_config: dict[str, int], result: dict) -> None:
    motors = {
        name: Motor(motor_id, "sts3215", MotorNormMode.RANGE_M100_100)
        for name, motor_id in motors_config.items()
    }
    bus = FeetechMotorsBus(port=port, motors=motors, calibration={})
    bus.connect(handshake=True)
    try:
        bus.disable_torque()
        for name in motors:
            bus.write("Operating_Mode", name, OperatingMode.POSITION.value, normalize=False)
        input(f"\n{port}: 托住手臂。把所有关节放到机械行程中间，确认没有卡住/拉扯后按 Enter...")
        homing = bus.set_half_turn_homings(list(motors))
        print(f"{port}: 现在逐关节缓慢走过真实可用行程，避开止挡与线缆。完成后按 Enter 停止记录...")
        range_min, range_max = bus.record_ranges_of_motion(list(motors))
        from lerobot.motors import MotorCalibration
        calibration = {
            name: MotorCalibration(
                id=motor.id,
                drive_mode=0,
                homing_offset=int(homing[name]),
                range_min=int(range_min[name]),
                range_max=int(range_max[name]),
            )
            for name, motor in motors.items()
        }
        bus.write_calibration(calibration)
        result.update({name: asdict(value) for name, value in calibration.items()})
    finally:
        try:
            bus.disconnect(disable_torque=True)
        except Exception as exc:
            print(f"{port} disconnect warning: {exc}", file=sys.stderr)


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    if CALIBRATION_FILE.exists():
        answer = input(f"已存在 {CALIBRATION_FILE}\n输入 overwrite 才允许重新校准：")
        if answer.strip().lower() != "overwrite":
            print("未修改校准文件。")
            return 0
    result = {}
    print("这是无动作校准：全程不启用扭矩，不移动机械臂。请手动引导各关节。")
    calibrate_port("COM3", JOINTS["COM3"], result)
    calibrate_port("COM4", JOINTS["COM4"], result)
    CALIBRATION_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("校准完成：", CALIBRATION_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
