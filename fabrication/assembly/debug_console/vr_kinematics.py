"""Hand-follow mapping for VR teleoperation; no hardware, no framework.

A grip press anchors the hand and the arm at their current poses. Afterwards
only the hand's displacement from that anchor moves the arm, so releasing and
gripping again is always a jump-free re-calibration (clutching).

Frames
  body   forward / left / up, from the headset yaw the client calibrated;
  arm    the arm's heading at anchor time counts as body forward, because the
         shoulder pan joint has no absolute zero on this robot;
  plane  the existing two-link plane of ``upstream_teleop``: radial x measured
         from the lift axis, height z.

The tool point is the jaw centre, so the gripper (not the wrist) follows the
hand and a hand pitch turns the jaws about that point.
"""
from __future__ import annotations

import math

from .upstream_teleop import L1, L2, O1, O2, forward

# Lift axis sits ahead of the pan axis: simulation/xlerobot URDF, Pitch joint origin.
PAN_OFFSET_M = .0306
# Wrist-pitch axis to the jaw centre. Estimated from the SO-101 model, not measured here.
TOOL_M = .10
# Tool elevation (degrees, up positive) = PSI_OFFSET - (lift + elbow + wrist pitch), using
# the calibration convention: forearm horizontal and wrist 0 at reference pose B.
PSI_OFFSET = math.degrees(O2 - O1)
MIN_REACH_M = .08
MAX_DELTA_M = .6


def wrap(deg):
    return (deg + 180.) % 360. - 180.


def inverse_near(x, z, lift):
    """The existing positive elbow branch, unwrapped near the grip pose.

    O2 is a calibration offset, not an extra elbow bend. Bounding b + O2
    by pi incorrectly rejected the real folded arm (about 98 degrees).
    Servo limits and per-step guards still bound every hardware target.
    """
    radius = math.hypot(x, z)
    if not abs(L1 - L2) + 1e-6 < radius < L1 + L2 - 1e-6:
        raise ValueError('末端超出二连杆工作空间')
    bend = math.acos(max(-1., min(1., (radius * radius - L1 * L1 - L2 * L2) / (2 * L1 * L2))))
    angle = math.atan2(z, x) + math.atan2(L2 * math.sin(bend), L1 + L2 * math.cos(bend))
    reference = math.radians(90 - lift) - O1
    angle += 2 * math.pi * round((reference - angle) / (2 * math.pi))
    if not -.1 <= angle + O1 <= 3.45:
        raise ValueError('末端逆解超过上游模型肩部范围')
    return 90 - math.degrees(angle + O1), math.degrees(bend + O2) - 90


def anchor(lift, elbow, wrist):
    """Geometry of the arm at grip time; raises ValueError outside the model."""
    x, z = forward(lift, elbow)
    reason = '当前折叠姿态不在跟随模型内，先用逐关节调整，再松开重握'
    try:
        lift2, elbow2 = inverse_near(x, z, lift)
    except ValueError as exc:
        raise ValueError(reason) from exc
    # The IK has one branch; a pose it cannot reproduce must not be followed.
    if abs(lift2 - lift) > 1 or abs(elbow2 - elbow) > 1:
        raise ValueError(reason)
    total = lift + elbow + wrist
    psi = PSI_OFFSET - total
    tip = (x + TOOL_M * math.cos(math.radians(psi)), z + TOOL_M * math.sin(math.radians(psi)))
    return {'x': x, 'z': z, 'sum': total, 'psi': psi, 'tip': tip, 'reach': PAN_OFFSET_M + tip[0], 'lift': lift}


def follow(geometry, delta, pitch=0.):
    """Joint angles for a hand displacement from the anchor.

    ``delta`` is (forward, left, up) in metres, ``pitch`` the tool elevation
    change in degrees (up positive). Returns (pan_left_deg, lift, elbow, wrist,
    at_edge): pan is relative to the anchor heading, the others are absolute
    model angles. A wrist target beyond the two-link reach is pulled back onto
    the reach boundary along the same direction and reported as ``at_edge``.
    Raises ValueError for a tracking jump or a pose the model cannot solve.
    """
    if any(abs(d) > MAX_DELTA_M for d in delta):
        raise ValueError('手柄位移超过0.6米，按追踪跳变丢弃')
    ahead = max(geometry['reach'] + delta[0], MIN_REACH_M)
    pan = math.degrees(math.atan2(delta[1], ahead))
    tip_x = math.hypot(ahead, delta[1]) - PAN_OFFSET_M
    tip_z = geometry['tip'][1] + delta[2]
    psi = geometry['psi'] + pitch
    wx = tip_x - TOOL_M * math.cos(math.radians(psi))
    wz = tip_z - TOOL_M * math.sin(math.radians(psi))
    reach = math.hypot(wx, wz)
    low, high = abs(L1 - L2) + 1e-3, L1 + L2 - 1e-3
    edge = not low <= reach <= high
    if edge:
        if reach < 1e-6:
            raise ValueError('末端目标落在肩部，无法求解')
        scale = min(high, max(low, reach)) / reach
        wx, wz = wx * scale, wz * scale
    try:
        lift, elbow = inverse_near(wx, wz, geometry['lift'])
    except ValueError as exc:
        raise ValueError('超出手臂工作空间') from exc
    return pan, lift, elbow, PSI_OFFSET - psi - lift - elbow, edge


def jaw_point(lift, elbow, wrist):
    """Jaw centre in the arm plane, for checks and the client preview."""
    x, z = forward(lift, elbow)
    psi = math.radians(PSI_OFFSET - lift - elbow - wrist)
    return x + TOOL_M * math.cos(psi), z + TOOL_M * math.sin(psi)
