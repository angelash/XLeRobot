"""Robot state and camera frames sampled on one clock, with a report of the timing actually achieved; read-only.

Reads the control service's status and newest camera snapshots. It sends no command and opens no device, so a
camera that nobody is viewing reports as missing. Whether a take is fit for imitation learning is judged from
its own timing; the limits used are proposals until reviewed. Frame age compares the control service's capture
time with this machine's clock, so it is meaningful only when both run on the same computer.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import sys
import time
import urllib.error
import urllib.request

STATE_KEYS = ('control_profile', 'controller', 'original', 'ports', 'gamepad', 'updated_at', 'input_age_ms')
LIMITS = {'min_rate_fraction': 0.9, 'max_late_fraction': 0.02, 'min_new_frame_fraction': 0.9,
          'max_frame_age_ticks': 1.5, 'max_camera_skew_ticks': 1.0}


def record(folder, *, state, frame, cameras=(0, 1, 2), fps=15, seconds=10, clock=time.monotonic, wall=time.time,
           sleep=time.sleep, stop=lambda: False):
    """Sample `state()` and `frame(camera)` every 1/fps s into `folder` and return the rows.

    `frame` returns (jpeg, captured_at, sequence) or raises when the camera has no current frame. A JPEG is
    stored once per sequence number, so a repeated frame shows in the rows instead of being counted as new.
    """
    if not 1 <= fps <= 30 or not 0 < seconds <= 600 or not cameras:
        raise ValueError('fps must be 1..30, seconds at most 600, and at least one camera is needed')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    period, start, last, rows, tick = 1/fps, clock(), {}, [], 0

    def received(camera):
        # Age is taken when this frame arrives, so a slow state read in the same tick is not counted against it.
        return frame(camera), wall()
    with ThreadPoolExecutor(len(cameras)) as pool, (folder/'frames.jsonl').open('w', encoding='utf-8') as out:
        while clock()-start < seconds and not stop():
            wait = start+tick*period-clock()
            if wait > 0:
                sleep(wait)
            began = clock()
            pending = {camera: pool.submit(received, camera) for camera in cameras}
            try:
                status, error = state(), None
            except Exception as exc:    # a failed read is a recorded fact of the take, not a reason to lose it
                status, error = {}, str(exc) or type(exc).__name__
            row = {'tick': tick, 't': round(began-start, 4), 'wall': wall(), 'state_ms': round((clock()-began)*1000, 1),
                   'state_error': error, 'state': {k: status[k] for k in STATE_KEYS if k in status}, 'cameras': {}}
            for camera, future in pending.items():
                try:
                    (jpeg, captured, sequence), arrived = future.result()
                except Exception as exc:
                    row['cameras'][str(camera)] = {'missing': str(exc) or type(exc).__name__}
                    continue
                new, name = last.get(camera) != sequence, f'cam{camera}/{sequence:08d}.jpg'
                if new:
                    (folder/name).parent.mkdir(exist_ok=True)
                    (folder/name).write_bytes(jpeg)
                    last[camera] = sequence
                row['cameras'][str(camera)] = {'sequence': sequence, 'captured_at': captured, 'new': new, 'file': name,
                                               'age_ms': round((arrived-captured)*1000, 1)}
            out.write(json.dumps(row, ensure_ascii=False)+'\n')
            rows.append(row)
            tick = max(tick+1, int((clock()-start)/period))     # a slot already past is skipped, not bunched up
    return rows


def _p95(values):
    values = sorted(values)
    return values[min(len(values)-1, int(.95*len(values)))] if values else None


def quality(rows, fps, cameras=(0, 1, 2), limits=LIMITS):
    """Timing a take actually achieved, and whether it meets the proposed limits for training data."""
    report = {'ticks': len(rows), 'requested_fps': fps, 'limits': limits, 'limits_status': 'proposed_not_reviewed',
              'robot_commands': 0, 'cameras': {}}
    if len(rows) < 2:
        return report | {'usable_for_training': False, 'reasons': ['样本不足两帧']}
    period, duration = 1/fps, rows[-1]['t']-rows[0]['t']
    gaps = [b['t']-a['t'] for a, b in zip(rows, rows[1:])]
    reasons = []
    report.update(duration_s=round(duration, 3), achieved_fps=round((len(rows)-1)/duration, 2),
                  late_fraction=round(sum(g > 1.5*period for g in gaps)/len(gaps), 4),
                  state_errors=sum(bool(r['state_error']) for r in rows), state_ms_p95=_p95([r['state_ms'] for r in rows]))
    if report['achieved_fps'] < limits['min_rate_fraction']*fps:
        reasons.append(f"实际采样率{report['achieved_fps']}帧/秒，低于要求的{fps}")
    if report['late_fraction'] > limits['max_late_fraction']:
        reasons.append(f"{report['late_fraction']:.1%}的节拍晚到")
    if report['state_errors']:
        reasons.append(f"{report['state_errors']}次状态读取失败")
    for camera in cameras:
        seen = [r['cameras'].get(str(camera), {}) for r in rows]
        got = [s for s in seen if 'sequence' in s]
        ages = [s['age_ms'] for s in got]
        entry = {'missing': len(seen)-len(got), 'new_frame_fraction': round(sum(s['new'] for s in got)/len(rows), 4),
                 'frame_fps': round(sum(s['new'] for s in got)/duration, 2),
                 'age_ms_median': statistics.median(ages) if ages else None, 'age_ms_p95': _p95(ages)}
        report['cameras'][str(camera)] = entry
        if entry['missing']:
            reasons.append(f"相机{camera}有{entry['missing']}拍没有画面")
        elif entry['new_frame_fraction'] < limits['min_new_frame_fraction']:
            reasons.append(f"相机{camera}只有{entry['new_frame_fraction']:.0%}的节拍拿到新帧（相机约{entry['frame_fps']}帧/秒）")
        elif entry['age_ms_p95'] > limits['max_frame_age_ticks']*period*1000:
            reasons.append(f"相机{camera}画面偏旧（95%分位{entry['age_ms_p95']}毫秒）")
    skew = [(max(times)-min(times))*1000 for times in
            ([r['cameras'][str(c)]['captured_at'] for c in cameras] for r in rows
             if all('sequence' in r['cameras'].get(str(c), {}) for c in cameras))]
    report['camera_skew_ms_median'] = round(statistics.median(skew), 1) if skew else None
    report['camera_skew_ms_p95'] = round(_p95(skew), 1) if skew else None
    if skew and report['camera_skew_ms_p95'] > limits['max_camera_skew_ticks']*period*1000:
        reasons.append(f"各相机拍摄时刻相差偏大（95%分位{report['camera_skew_ms_p95']}毫秒）")
    return report | {'usable_for_training': not reasons, 'reasons': reasons}


def http_sources(controller, timeout=2.0):
    """`state` and `frame` readers for the control service; GET requests only, system proxies ignored."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def state():
        with opener.open(controller+'/api/original/status', timeout=timeout) as response:
            return json.loads(response.read())

    def frame(camera):
        try:
            with opener.open(f'{controller}/api/camera/{camera}/snapshot', timeout=timeout) as response:
                return response.read(), float(response.headers['X-Captured-At']), int(response.headers['X-Frame-Sequence'])
        except urllib.error.HTTPError as exc:
            raise LookupError(f'HTTP {exc.code}：相机当前没有人在看，先在 Studio 打开这路画面') from exc
    return state, frame


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['probe'])
    parser.add_argument('--root', required=True, help='private folder for the take; never a tracked path')
    parser.add_argument('--controller', default='http://127.0.0.1:8765')
    parser.add_argument('--fps', type=int, default=15)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--cameras', type=int, nargs='+', default=[0, 1, 2], choices=[0, 1, 2])
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    state, frame = http_sources(args.controller.rstrip('/'))
    folder = Path(args.root)/('take-'+time.strftime('%Y%m%d-%H%M%S'))
    rows = record(folder, state=state, frame=frame, cameras=tuple(args.cameras), fps=args.fps, seconds=args.seconds)
    report = quality(rows, args.fps, tuple(args.cameras)) | {'folder': str(folder), 'controller': args.controller}
    (folder/'quality.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
