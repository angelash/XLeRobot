"""Confirm suspect temperature samples on the same motor, without cached values.

The user classifies isolated temperature jumps as sensor/readout anomalies.
One fresh normal packet within three reads clears that episode. Recovered
episodes have no lifetime/count stop threshold; persistent heat/invalid data
and other packet faults still reach the caller's existing stop path.
"""
from __future__ import annotations

POLICY_VERSION = 'same-motor-fresh-normal-v1'


def normal_temperature(value, stop_c):
    return 1 <= value < stop_c


def confirm_temperature(reg, reread, stop_c, *, other_ok=lambda reg: True,
                        check_abort=lambda: None):
    """Return a complete fresh packet and, for a suspect sample, raw evidence.

    ``reread`` must address the same motor under the caller's existing IO lock.
    Never wait, retry transport errors, substitute old temperatures, or discard
    a packet with a different fault. At most two extra reads are performed.
    """
    if normal_temperature(reg[63], stop_c) or not other_ok(reg):
        return reg, None
    samples = [list(reg)]
    for _ in range(2):
        check_abort()
        reg = reread()
        samples.append(list(reg))
        if not other_ok(reg) or normal_temperature(reg[63], stop_c):
            break
    recovered = other_ok(reg) and normal_temperature(reg[63], stop_c)
    return reg, {'kind': 'temperature', 'recovered': recovered,
                 'first': samples[0], 'reread': samples[-1], 'samples': samples,
                 'stop_c': stop_c}
