from types import SimpleNamespace
import pytest
from fabrication.assembly.debug_console.serial_identity import resolve_ports
from fabrication.assembly.debug_console import torque_control as torque


def devices():
    return [SimpleNamespace(device='COM17',serial_number='5B61034756'),
            SimpleNamespace(device='COM8',serial_number='5B61034338')]


def test_new_windows_port_numbers_preserve_bus_roles():
    assert resolve_ports(devices())=={'COM3':'COM17','COM4':'COM8'}


def test_old_com_number_with_wrong_board_is_rejected():
    with pytest.raises(RuntimeError):resolve_ports([SimpleNamespace(device='COM3',serial_number='different')])


def test_missing_or_duplicate_identity_never_guesses():
    with pytest.raises(RuntimeError):resolve_ports(devices()[:1])
    with pytest.raises(RuntimeError):resolve_ports(devices()+devices()[:1])


def test_transport_maps_ports_and_sets_modem_lines_before_open(monkeypatch):
    events=[]
    class Port:
        def __init__(self,**kwargs):self.port=kwargs['port'];self.is_open=False;self.dtr=True;self.rts=True
        def open(self):
            assert self.dtr is False and self.rts is False
            events.append(('open',self.port));self.is_open=True
        def close(self):events.append(('close',self.port));self.is_open=False
    monkeypatch.setattr(torque,'resolve_ports',lambda:resolve_ports(devices()))
    monkeypatch.setattr(torque.serial,'Serial',Port)
    session=torque.TorqueSession();session.open()
    assert session.ports['COM3'].port=='COM17' and session.ports['COM4'].port=='COM8'
    session.open();assert len(events)==2
    session.close();assert len(events)==4
