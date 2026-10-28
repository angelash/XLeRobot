"""Hold-position torque control for the connected XLeRobot buses.

This module writes torque and goal registers. It never imports the motion
controller and never commands a relative move. Enabling torque first captures
the present position and writes that position back as the goal.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import serial


PORTS = ("COM3", "COM4")
EXPECTED = {"COM3": list(range(1, 9)), "COM4": [1, 2, 3, 4, 5, 6, 9, 10]}
BAUD = 1_000_000
SCAN_IDS = range(1, 21)
POSITION_MODE = 0
POSITION_P = 12
POSITION_I = 0
POSITION_D = 32
EVIDENCE = Path(__file__).resolve().parents[1] / "evidence" / "2026-10-28"


def checksum(body: list[int]) -> int:
    return (~sum(body)) & 0xFF


def packet(motor_id: int, instruction: int, params: list[int] | None = None) -> bytes:
    if instruction not in (1, 2, 3):
        raise ValueError("only PING, READ and WRITE are allowed")
    if not 1 <= motor_id <= 253:
        raise ValueError("broadcast IDs are forbidden")
    body = [motor_id, len(params or []) + 2, instruction, *(params or [])]
    return bytes([0xFF, 0xFF, *body, checksum(body)])


def exchange(port: serial.Serial, motor_id: int, instruction: int, params: list[int] | None = None) -> dict[str, Any] | None:
    port.reset_input_buffer()
    port.write(packet(motor_id, instruction, params))
    deadline = time.monotonic() + 0.12
    buffer = bytearray()
    while time.monotonic() < deadline:
        waiting = port.in_waiting
        if waiting:
            buffer.extend(port.read(waiting))
        for start in range(max(0, len(buffer) - 400), len(buffer) - 5):
            if buffer[start:start + 2] != b"\xff\xff" or buffer[start + 2] != motor_id:
                continue
            length = buffer[start + 3]
            end = start + length + 4
            if end > len(buffer):
                continue
            frame = buffer[start:end]
            if sum(frame[2:]) & 0xFF != 0xFF:
                continue
            return {"error": frame[4], "data": list(frame[5:-1])}
        time.sleep(0.002)
    return None


def read_registers(port: serial.Serial, motor_id: int) -> list[int]:
    reply = exchange(port, motor_id, 2, [0, 70])
    if reply is None or reply["error"] != 0 or len(reply["data"]) != 70:
        raise RuntimeError(f"ID {motor_id} 状态读取失败")
    return reply["data"]


def write_register(port: serial.Serial, motor_id: int, address: int, value: int, length: int) -> None:
    if length == 1:
        params = [address, value & 0xFF]
    elif length == 2:
        params = [address, value & 0xFF, (value >> 8) & 0xFF]
    else:
        raise ValueError("unsupported register length")
    reply = exchange(port, motor_id, 3, params)
    if reply is None or reply["error"] != 0:
        raise RuntimeError(f"ID {motor_id} 写入地址 {address} 失败")


def word(registers: list[int], address: int) -> int:
    return registers[address] + 256 * registers[address + 1]


def discover(port: serial.Serial) -> list[int]:
    found = []
    for motor_id in SCAN_IDS:
        reply = exchange(port, motor_id, 1)
        if reply is not None and reply["error"] == 0:
            found.append(motor_id)
        time.sleep(0.004)
    return found


class TorqueSession:
    """One serial owner for both boards."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.ports: dict[str, serial.Serial] = {}

    def open(self) -> None:
        for name in PORTS:
            if name in self.ports and self.ports[name].is_open:
                continue
            port = serial.Serial(port=name, baudrate=BAUD, timeout=0.12, write_timeout=0.12)
            port.dtr = False
            port.rts = False
            self.ports[name] = port

    def close(self) -> None:
        for port in self.ports.values():
            if port.is_open:
                port.close()
        self.ports.clear()

    def apply(self, enabled: bool, selection: dict[str, list[int]] | None = None) -> dict[str, Any]:
        with self.lock:
            self.open()
            selected = selection or EXPECTED
            missing = []
            for name, motor_ids in selected.items():
                found = set(discover(self.ports[name]))
                missing.extend(f"{name}:{motor_id}" for motor_id in motor_ids if motor_id not in found)
            if missing:
                raise RuntimeError("未完整扫描到电机：" + ", ".join(missing))
            results = []
            for name, motor_ids in selected.items():
                port = self.ports[name]
                for motor_id in motor_ids:
                    results.append(self._apply_one(port, name, motor_id, enabled))
            report = {
                "enabled": enabled,
                "count": len(results),
                "motors": results,
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            EVIDENCE.mkdir(parents=True, exist_ok=True)
            (EVIDENCE / "torque-command.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return report

    def _apply_one(self, port: serial.Serial, port_name: str, motor_id: int, enabled: bool) -> dict[str, Any]:
        before = read_registers(port, motor_id)
        position = word(before, 56)
        if enabled:
            # Lock the servo at its current pose before torque is applied.
            write_register(port, motor_id, 40, 0, 1)
            write_register(port, motor_id, 33, POSITION_MODE, 1)
            write_register(port, motor_id, 21, POSITION_P, 1)
            write_register(port, motor_id, 22, POSITION_D, 1)
            write_register(port, motor_id, 23, POSITION_I, 1)
            write_register(port, motor_id, 46, 0, 2)
            write_register(port, motor_id, 42, position, 2)
            write_register(port, motor_id, 40, 1, 1)
        else:
            write_register(port, motor_id, 46, 0, 2)
            write_register(port, motor_id, 40, 0, 1)
        after = read_registers(port, motor_id)
        return {
            "port": port_name,
            "id": motor_id,
            "held_position": position,
            "torque": after[40],
            "mode": after[33],
            "goal_position": word(after, 42),
            "present_position": word(after, 56),
            "goal_velocity": word(after, 46),
            "voltage": after[62] / 10,
            "temperature": after[63],
            "error": 0,
        }
