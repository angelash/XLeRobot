"""Hardware-derived auto-enable behavior and safe trial-range transactions."""
import pytest
from fabrication.assembly.debug_console import commissioning as mod
from fabrication.assembly.debug_console.test_control_runtime import ServoPort
from fabrication.assembly.debug_console.torque_control import word


def port_at(position=2050):
    p=ServoPort([1]);r=p.regs[1];r[40]=0;r[56:58]=[position&255,position>>8]
    return p


def test_backup_precedes_mutation_and_goal_has_zero_output():
    p=port_at();saved=[];original=p.write;goal_limits=[]
    def write(packet):
        if packet[4]==3:
            assert saved,'No mutating packet may precede a persisted backup'
            if packet[5]==42:goal_limits.append(word(p.regs[1],48))
        return original(packet)
    p.write=write
    result=mod.configure_test_window(p,1,lambda r:saved.append(r))
    assert goal_limits and set(goal_limits)=={0}
    assert result['full_manual_range_verified'] is False
    assert (word(p.regs[1],9),word(p.regs[1],11))==(2018,2082)
    assert p.regs[1][40]==0 and word(p.regs[1],48)==1000
    assert word(p.regs[1],56)==2050
    assert p.regs[1][13:40]==saved[0][13:40]


@pytest.mark.parametrize('position',[1500,2500])
def test_stale_disjoint_range_can_be_replaced_without_invalid_intermediate_range(position):
    p=port_at(position);original=p.write
    def write(packet):
        if packet[4]==3 and packet[5] in (9,11):
            value=packet[6]+256*packet[7];r=p.regs[1]
            assert value<word(r,11) if packet[5]==9 else word(r,9)<value
        return original(packet)
    p.write=write
    mod.configure_test_window(p,1,lambda _:None)
    assert p.regs[1][40]==0 and word(p.regs[1],56)==position


def test_lost_goal_ack_restores_original_configuration_and_keeps_torque_off(monkeypatch):
    p=port_at();before=list(p.regs[1]);original=mod.write_register;failed=[]
    def write(port,i,address,value,length):
        original(port,i,address,value,length)
        if address==42 and not failed:
            failed.append(True);assert port.regs[i][40]==1
            raise RuntimeError('goal applied but ACK lost')
    monkeypatch.setattr(mod,'write_register',write)
    with pytest.raises(RuntimeError,match='ACK lost'):mod.configure_test_window(p,1,lambda _:None)
    assert p.regs[1][:40]==before[:40]
    assert p.regs[1][40]==0 and word(p.regs[1],48)==word(before,48)
    assert word(p.regs[1],56)==word(before,56)


def test_failed_output_restore_retains_zero_limit_and_torque_off(monkeypatch):
    p=port_at();original=mod.write_register
    def write(port,i,address,value,length):
        if address==48 and value==1000:raise RuntimeError('output restore ACK lost')
        return original(port,i,address,value,length)
    monkeypatch.setattr(mod,'write_register',write)
    with pytest.raises(RuntimeError,match='回滚未确认'):mod.configure_test_window(p,1,lambda _:None)
    assert p.regs[1][40]==0 and word(p.regs[1],48)==0


@pytest.mark.parametrize('bad',['torque','model','mode','temperature','voltage'])
def test_invalid_initial_state_never_writes(bad):
    p=port_at();r=p.regs[1]
    if bad=='torque':r[40]=1
    elif bad=='model':r[3:5]=[0,0]
    elif bad=='mode':r[33]=1
    elif bad=='temperature':r[63]=50
    elif bad=='voltage':r[62]=90
    with pytest.raises(ValueError):mod.configure_test_window(p,1,lambda _:None)
    assert not p.writes


def test_failed_backup_never_writes():
    p=port_at()
    def failed(_):raise OSError('backup disk full')
    with pytest.raises(OSError):mod.configure_test_window(p,1,failed)
    assert not p.writes


def test_explicit_factory_gain_comparison_and_full_rollback():
    p=port_at();p.regs[1][21]=16;p.regs[1][22]=43;before=list(p.regs[1])
    mod.configure_test_window(p,1,lambda _:None,position_gains=(32,32))
    assert p.regs[1][21:23]==[32,32] and p.regs[1][40]==0
    mod.restore_test_window(p,1,before)
    assert p.regs[1][:40]==before[:40] and p.regs[1][40]==0


def test_arbitrary_gain_changes_are_rejected_before_writes():
    p=port_at()
    with pytest.raises(ValueError):mod.configure_test_window(p,1,lambda _:None,position_gains=(254,0))
    assert not p.writes
