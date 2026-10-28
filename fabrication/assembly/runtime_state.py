"""Atomic, local-only state bridge between the motion controller and Web UI."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any


STATE_FILE = Path(__file__).resolve().parent / "evidence" / "2026-10-27" / "robot-runtime-state.json"


def write_state(event: str, **fields: Any) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.is_file() else {}
    except (OSError, ValueError):
        previous = {}
    previous.update(fields)
    if event == "heartbeat":
        # Heartbeats must not erase a more specific recent action.
        previous.pop("last_action", None)
    previous["event"] = event
    previous["updated_at"] = dt.datetime.now().astimezone().isoformat()
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, STATE_FILE)


def read_state() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return {
            "event": "never_started",
            "controller": "offline",
            "torque": False,
            "active_joint": None,
            "selected_joint": None,
            "last_action": "尚无动作",
            "updated_at": None,
        }
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"controller": "state_error", "event": "state_error", "error": str(exc)}
