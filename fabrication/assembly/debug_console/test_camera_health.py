import threading
from types import SimpleNamespace


def test_camera_health_updates_while_motor_bus_is_locked(monkeypatch):
    from fabrication.assembly.debug_console import app as web
    bus=threading.Lock()
    initial={'camera':0,'state':'connecting','frames':0}
    fake=SimpleNamespace(io=bus,camera_health={0:initial})
    monkeypatch.setattr(web,'runtime',fake)
    finished=threading.Event()
    def publish():
        web.set_camera(0,'online',20)
        finished.set()
    with bus:
        worker=threading.Thread(target=publish,daemon=True);worker.start()
        independent=finished.wait(.5)
    worker.join(timeout=1)
    assert independent, 'Motor I/O must not stall camera frame publication'
    assert fake.camera_health[0]=={'camera':0,'state':'online','frames':20}
    assert initial=={'camera':0,'state':'connecting','frames':0}, 'A returned status row remains coherent'


def test_state_only_camera_update_preserves_last_counter(monkeypatch):
    from fabrication.assembly.debug_console import app as web
    fake=SimpleNamespace(camera_health={2:{'camera':2,'state':'online','frames':40}})
    monkeypatch.setattr(web,'runtime',fake)
    web.set_camera(2,'offline')
    assert fake.camera_health[2]=={'camera':2,'state':'offline','frames':40}
