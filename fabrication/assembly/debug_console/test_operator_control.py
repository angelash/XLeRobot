from copy import deepcopy
import threading
import time

import pytest

from fabrication.assembly.debug_console.operator_control import LinkedModeUnavailable, NativeMenuControl, OperatorEvents
from fabrication.assembly.debug_console.test_original_teleop import pad
from fabrication.assembly.debug_console.test_whole_calibration import runtime  # noqa: F401


def sample(runtime, raw):
    runtime.gamepad=deepcopy(raw);runtime.pad_time=time.monotonic()
    return runtime.native_pad_event(raw)


def menu(runtime):
    sample(runtime,pad());sample(runtime,pad(buttons=(7,)));sample(runtime,pad())


@pytest.fixture
def queued(runtime):
    jobs=[];runtime.native_menu=NativeMenuControl(runtime,spawn=jobs.append)
    return runtime,jobs


def test_connected_pad_and_menu_held_at_connect_never_start_a_session(queued):
    r,jobs=queued
    for _ in range(10):sample(r,pad(buttons=(7,)))
    sample(r,pad())
    assert not jobs and r.controller['controller']=='offline'
    sample(r,pad(buttons=(7,)))
    assert not jobs and r.operator_events.read()['phase']=='waiting-release'
    for _ in range(10):sample(r,pad(buttons=(7,)))
    sample(r,pad())
    assert len(jobs)==1 and r.operator_events.read()['phase']=='starting'


def test_real_preparation_falls_back_only_for_missing_linked_setup_and_holds_current(queued):
    r,jobs=queued
    before={(p,i):list(port.regs[i][56:58]) for p,port in r.session.ports.items() for i in port.regs}
    menu(r);jobs.pop()()
    assert r.control_profile=='original_preset' and r.controller['controller']=='online'
    state=r.operator_events.read()
    assert state['phase']=='active' and state['fallback_reason'] and state['preferred_mode']=='hardware'
    for (p,i),position in before.items():
        if p=='COM4' and i in (9,10):continue
        assert r.session.ports[p].regs[i][56:58]==position
    sample(r,pad(buttons=(7,)))
    assert len(jobs)==1
    for _ in range(8):sample(r,pad(buttons=(7,)))
    assert len(jobs)==1
    jobs.pop()()
    assert r.controller['controller']=='offline'
    sample(r,pad())
    assert not jobs


def test_linked_mode_wins_when_ready_and_unexpected_errors_do_not_fall_back(queued,monkeypatch):
    r,jobs=queued;calls=[]
    def linked(confirmed):
        calls.append('linked');r.control_profile='original_hardware';r.controller.update(controller='online',paused=False)
    monkeypatch.setattr(r,'begin_whole_robot',linked)
    monkeypatch.setattr(r,'begin_preset',lambda _:calls.append('preset'))
    menu(r);jobs.pop()()
    assert calls==['linked'] and r.operator_events.read()['mode']=='original_hardware'
    r._stop('generated test stop');sample(r,pad())
    def fail(_):raise OSError('generated serial fault')
    monkeypatch.setattr(r,'begin_whole_robot',fail)
    menu(r);jobs.pop()()
    assert calls==['linked'] and r.controller['controller']=='offline'
    assert 'serial fault' in r.operator_events.read()['reason']


@pytest.mark.parametrize('change',[pad([.5,0,0,0,0,0]),pad(buttons=(6,)),{'connected':False}])
def test_queued_preparation_can_be_cancelled_without_hardware_start(queued,monkeypatch,change):
    r,jobs=queued;calls=[]
    monkeypatch.setattr(r,'begin_whole_robot',lambda _:calls.append('start'))
    menu(r);sample(r,change);jobs.pop()()
    assert not calls and r.controller['controller']=='offline'
    assert r.operator_events.read()['phase']=='blocked'


def test_second_menu_cancels_preparation_and_deflected_start_is_rejected(queued,monkeypatch):
    r,jobs=queued;calls=[]
    monkeypatch.setattr(r,'begin_whole_robot',lambda _:calls.append('start'))
    sample(r,pad());sample(r,pad([.5,0,0,0,0,0],buttons=(7,)))
    sample(r,pad())
    assert not jobs
    menu(r);sample(r,pad(buttons=(7,)));sample(r,pad());jobs.pop()()
    assert not calls


def test_disconnect_ends_control_and_reconnect_never_replays_or_restarts(queued,monkeypatch):
    r,jobs=queued
    monkeypatch.setattr(r,'begin_whole_robot',lambda _:r.controller.update(controller='online',paused=False))
    menu(r);jobs.pop()()
    sample(r,{'connected':False});assert len(jobs)==1
    sample(r,pad(buttons=(7,)));jobs.pop()()
    sample(r,pad());assert not jobs and r.controller['controller']=='offline'


def test_task_and_manual_collection_conflicts_never_become_fallback(queued,monkeypatch):
    r,jobs=queued;calls=[]
    monkeypatch.setattr(r,'begin_whole_robot',lambda _:calls.append('start'))
    monkeypatch.setattr(r,'_require_not_calibrating',lambda:(_ for _ in ()).throw(ValueError('generated collection busy')))
    menu(r);jobs.pop()()
    assert not calls and 'collection busy' in r.operator_events.read()['reason']


def test_preparation_keeps_sampling_fresh_and_rechecks_neutral_before_accepting(queued,monkeypatch):
    r,jobs=queued;entered=threading.Event();release=threading.Event()
    def linked(_):
        entered.set();assert release.wait(3)
        r.controller.update(controller='online',paused=False);r.control_profile='original_hardware'
    monkeypatch.setattr(r,'begin_whole_robot',linked)
    menu(r);worker=threading.Thread(target=jobs.pop());worker.start();assert entered.wait(2)
    start=time.monotonic()
    sample(r,pad([.6,0,0,0,0,0]))
    assert time.monotonic()-start<.2
    assert time.monotonic()-r.pad_time<.2
    release.set();worker.join(3);assert not worker.is_alive()
    assert r.controller['controller']=='offline' and '取消' in r.operator_events.read()['reason']


def test_operator_events_deduplicate_and_wake_subscribers():
    events=OperatorEvents();events.publish({'connected':False})
    initial=events.read();events.publish({'connected':False})
    assert events.read()==initial
    events.publish({'connected':True})
    assert events.wait(initial['seq'],.01)['connected'] is True
    assert events.read()['epoch']==initial['epoch'] and events.read()['seq']==initial['seq']+1
