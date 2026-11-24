import json
import threading
import pytest
from deployment.demos import sync


class Rig:
    """A clock that only moves when the recorder sleeps or a read takes time, with cameras of a given frame rate."""

    def __init__(self, camera_fps=30., state_s=.004, dead=(), state_fails_at=()):
        self.now, self.camera_fps, self.state_s, self.dead, self.calls = 100., camera_fps, state_s, dead, 0
        self.state_fails_at = state_fails_at

    def clock(self):
        return self.now

    def wall(self):
        return 5000.+self.now

    def sleep(self, seconds):
        self.now += seconds

    def state(self):
        self.calls += 1
        self.now += self.state_s
        if self.calls in self.state_fails_at:
            raise OSError('control service unreachable')
        return {'original': {'active': True}, 'ports': [{'port': 'COM5'}], 'updated_at': self.now, 'secret': 'not copied'}

    def frame(self, camera):
        if camera in self.dead:
            raise LookupError('no viewer')
        sequence = int(self.now*self.camera_fps+camera*.3)
        return b'\xff\xd8'+bytes([camera])+b'\xff\xd9', 5000.+sequence/self.camera_fps, sequence

    def take(self, folder, **options):
        rows = sync.record(folder, state=self.state, frame=self.frame, clock=self.clock, wall=self.wall, sleep=self.sleep,
                           **{'fps': 15, 'seconds': 2} | options)
        return rows, sync.quality(rows, options.get('fps', 15), options.get('cameras', (0, 1, 2)))


def test_a_steady_take_with_fast_cameras_is_usable_and_keeps_one_file_per_frame(tmp_path):
    rows, report = Rig().take(tmp_path)
    assert report['usable_for_training'] and report['reasons'] == [] and report['robot_commands'] == 0
    assert report['achieved_fps'] == pytest.approx(15, abs=.2) and report['late_fraction'] == 0
    assert all(report['cameras'][c]['new_frame_fraction'] == 1 and report['cameras'][c]['missing'] == 0 for c in '012')
    assert len(list((tmp_path/'cam1').glob('*.jpg'))) == len(rows) and len(rows) in (30, 31)
    saved = [json.loads(line) for line in (tmp_path/'frames.jsonl').read_text(encoding='utf-8').splitlines()]
    assert saved == rows and set(saved[0]['state']) == {'original', 'ports', 'updated_at'}
    assert report['limits_status'] == 'proposed_not_reviewed'


def test_cameras_slower_than_the_sampling_rate_are_reported_and_repeats_are_not_stored_again(tmp_path):
    rows, report = Rig(camera_fps=5).take(tmp_path)
    # One reason per camera, plus the capture-time spread that cameras this slow cannot avoid.
    assert not report['usable_for_training'] and len(report['reasons']) == 4
    assert report['cameras']['0']['new_frame_fraction'] == pytest.approx(1/3, abs=.05)
    assert report['cameras']['0']['frame_fps'] == pytest.approx(5, abs=.6)
    assert len(list((tmp_path/'cam0').glob('*.jpg'))) == sum(r['cameras']['0']['new'] for r in rows) < len(rows)/2


def test_a_camera_nobody_views_and_a_failed_state_read_are_recorded_not_hidden(tmp_path):
    rows, report = Rig(dead=(2,), state_fails_at=(4,)).take(tmp_path)
    assert not report['usable_for_training']
    assert report['cameras']['2']['missing'] == len(rows) and 'no viewer' in rows[0]['cameras']['2']['missing']
    assert report['state_errors'] == 1 and rows[3]['state'] == {} and 'unreachable' in rows[3]['state_error']
    assert report['camera_skew_ms_median'] is None and not (tmp_path/'cam2').exists()


def test_a_slow_state_read_shows_as_a_low_rate_with_skipped_slots(tmp_path):
    rows, report = Rig(state_s=.15).take(tmp_path)
    assert not report['usable_for_training'] and report['achieved_fps'] < 8 and report['late_fraction'] > .9
    assert [r['tick'] for r in rows[:3]] == [0, 2, 4]


def test_frame_age_is_taken_when_the_frame_arrives_not_after_a_slow_state_read(tmp_path):
    # Seen on the vehicle 2026-11-24: a ~280 ms status read made every 10-30 ms snapshot look 200-340 ms old.
    rig, fetched, main = Rig(state_s=.15), threading.Semaphore(0), threading.current_thread()

    def wall():
        value = rig.wall()
        if threading.current_thread() is not main:     # a camera reader stamping its arrival
            fetched.release()
        return value

    def state():                                       # the slow read starts only after all three frames arrived
        for _ in range(3):
            assert fetched.acquire(timeout=5)
        return rig.state()
    rows = sync.record(tmp_path, state=state, frame=rig.frame, clock=rig.clock, wall=wall, sleep=rig.sleep, fps=15, seconds=2)
    report = sync.quality(rows, 15)
    assert all(report['cameras'][c]['age_ms_median'] < 100 for c in '012')
    assert report['state_ms_p95'] >= 150 and not report['usable_for_training']


def test_stop_and_bad_arguments(tmp_path):
    rig = Rig()
    rows = sync.record(tmp_path, state=rig.state, frame=rig.frame, clock=rig.clock, wall=rig.wall, sleep=rig.sleep,
                       stop=lambda: rig.calls >= 3)
    assert len(rows) == 3 and not sync.quality(rows[:1], 15)['usable_for_training']
    for bad in ({'fps': 0}, {'fps': 31}, {'seconds': 0}, {'cameras': ()}):
        with pytest.raises(ValueError):
            sync.record(tmp_path, state=rig.state, frame=rig.frame, **bad)


def test_command_line_writes_the_report_beside_the_take(tmp_path, monkeypatch, capsys):
    rig = Rig()
    monkeypatch.setattr(sync, 'http_sources', lambda controller: (rig.state, rig.frame))
    monkeypatch.setattr(sync.time, 'monotonic', rig.clock)
    monkeypatch.setattr(sync.time, 'time', rig.wall)
    monkeypatch.setattr(sync.time, 'sleep', rig.sleep)
    monkeypatch.setattr(sync, 'record', lambda folder, **options: sync_record(folder, clock=rig.clock, wall=rig.wall,
                                                                                sleep=rig.sleep, **options))
    assert sync.main(['probe', '--root', str(tmp_path), '--seconds', '1', '--cameras', '0', '1']) == 0
    printed = json.loads(capsys.readouterr().out)
    folder = next(tmp_path.glob('take-*'))
    assert printed['usable_for_training'] and set(printed['cameras']) == {'0', '1'}
    assert json.loads((folder/'quality.json').read_text(encoding='utf-8')) == printed


sync_record = sync.record
