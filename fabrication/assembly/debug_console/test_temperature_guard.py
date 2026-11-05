"""Temperature-only recovery must neither reuse stale values nor mask faults."""
import pytest
from fabrication.assembly.debug_console.temperature_guard import confirm_temperature


def packet(temp, status=0):
    reg=[0]*70;reg[63]=temp;reg[65]=status
    return reg


@pytest.mark.parametrize('temps',[(92,33),(52,46,33),(0,255,34)])
def test_one_fresh_normal_sample_clears_the_episode(temps):
    pending=iter(packet(t) for t in temps[1:])
    reg,evidence=confirm_temperature(packet(temps[0]),lambda:next(pending),45)
    assert reg[63]==temps[-1] and evidence['recovered']
    assert [r[63] for r in evidence['samples']]==list(temps)


@pytest.mark.parametrize('temp',[0,45,57,255])
def test_persistent_heat_or_invalid_data_is_not_cleared(temp):
    calls=[]
    def read():
        calls.append(1);return packet(temp)
    reg,evidence=confirm_temperature(packet(temp),read,45)
    assert not evidence['recovered'] and reg[63]==temp and len(calls)==2


def test_no_extra_reads_for_normal_temperature():
    def unexpected():raise AssertionError('unnecessary IO')
    assert confirm_temperature(packet(33),unexpected,45)==(packet(33),None)


def test_normal_temperature_cannot_discard_another_packet_fault():
    reg,evidence=confirm_temperature(packet(92),lambda:packet(33,32),45,
                                     other_ok=lambda r:not r[65])
    assert reg[65]==32 and not evidence['recovered']


def test_transport_error_is_not_retried_or_treated_as_normal():
    def failed():raise RuntimeError('bus lost')
    with pytest.raises(RuntimeError,match='bus lost'):
        confirm_temperature(packet(92),failed,45)


def test_abort_is_checked_before_each_extra_read():
    def abort():raise RuntimeError('operator stop')
    def unexpected():raise AssertionError('read after stop')
    with pytest.raises(RuntimeError,match='operator stop'):
        confirm_temperature(packet(92),unexpected,45,check_abort=abort)
