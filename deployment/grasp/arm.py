"""Planar model of one XLeRobot SO-101 arm plus shoulder pan.

Zero is the recorded upright pose (lift 2211, elbow 955, wrist pitch 2245: the
whole arm pointing straight up); increasing raw counts tilt each link forward
and down. Link lengths are the SO-101 nominal ones. It is a model calibrated on
2026-11-03 photos, not a measurement: the skill re-anchors every decision on
the servo readback after each step. Units: cm and degrees; x forward from the
shoulder-lift axis, z up from it.
"""
from __future__ import annotations

import math

K = 360/4096
L1, L2, L3 = 11.25, 13.5, 10.0
REF = {'lift': 2211, 'elbow': 955, 'pitch': 2245}
PAN_REF = 2037          # folded left-arm pan centre (agent_ops.PAN_CENTRE)
UPRIGHT = (2211, 955, 2245)
HIGH_OVER_DESK = (2522, 1513, 2247)   # verified 2026-11-03, between upright and the desk


class Unreachable(ValueError):
    pass


def fk(lift, elbow, pitch):
    t1 = (lift - REF['lift'])*K
    t2 = t1 + (elbow - REF['elbow'])*K
    t3 = t2 + (pitch - REF['pitch'])*K
    r = math.radians
    wx = L1*math.sin(r(t1)) + L2*math.sin(r(t2))
    wz = L1*math.cos(r(t1)) + L2*math.cos(r(t2))
    return {'tip': (wx + L3*math.sin(r(t3)), wz + L3*math.cos(r(t3))), 'wrist': (wx, wz), 'elbow_pt': (L1*math.sin(r(t1)), L1*math.cos(r(t1))),
            't1': t1, 't2': t2, 't3': t3, 'bend': t2 - t1, 'reach': math.hypot(wx, wz)}


def ik(x, z, t3, margin=0.3, min_bend=18.0):
    """Joint raws putting the tip at (x, z) with the gripper axis t3 degrees from vertical."""
    r = math.radians
    wx, wz = x - L3*math.sin(r(t3)), z - L3*math.cos(r(t3))
    d = math.hypot(wx, wz)
    if d > L1 + L2 - margin or d < abs(L2 - L1) + 1:
        raise Unreachable(f'腕点距肩{d:.1f}cm，超出可达范围')
    bend = math.degrees(math.acos(max(-1.0, min(1.0, (d*d - L1*L1 - L2*L2)/(2*L1*L2)))))
    if bend < min_bend:
        raise Unreachable(f'肘弯{bend:.1f}°，过于接近伸直')
    phi = math.degrees(math.atan2(wx, wz))
    a = math.degrees(math.acos(max(-1.0, min(1.0, (L1*L1 + d*d - L2*L2)/(2*L1*d)))))
    t1 = phi - a
    t2 = t1 + bend
    return (round(REF['lift'] + t1/K), round(REF['elbow'] + bend/K), round(REF['pitch'] + (t3 - t2)/K))


def segment_min_z(a, b, samples=20):
    """Lowest tip and wrist height while the joints interpolate linearly from a to b."""
    low_tip = low_wrist = 1e9
    for k in range(samples + 1):
        t = k/samples
        q = [a[i] + (b[i] - a[i])*t for i in range(3)]
        f = fk(*q)
        low_tip = min(low_tip, f['tip'][1]); low_wrist = min(low_wrist, f['wrist'][1])
    return low_tip, low_wrist


def pan_angle(pan):
    """Degrees, positive to the robot's left (pan raw decreasing)."""
    return (PAN_REF - pan)*K


def lateral_to_pan(cm, radius):
    """Pan raw change that moves the gripper cm to the left at a given radius."""
    return -round(math.degrees(cm/max(radius, 5.0))/K)
