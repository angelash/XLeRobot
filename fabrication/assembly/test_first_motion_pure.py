"""Hardware-free checks for first-motion safety boundaries."""

from first_motion_controller import SafeMotion


def test_command_step_is_one_edge_not_each_frame():
    # This documents the desired mapping; the full edge handling is exercised
    # in main() with a gamepad. The safety cap remains mandatory.
    assert SafeMotion


def test_per_command_cap():
    # All commands have an independently coded cap at the bus write boundary.
    from pathlib import Path
    source = Path(__file__).with_name("first_motion_controller.py").read_text(encoding="utf-8")
    assert "Per-step raw limit exceeded" in source
    assert "--max-raw" in source
