"""Audit the archived 2026-11-16 body scan. No network or actuator calls.

Input: exported spin-NNN/ and adjust-NNN/result.json, source JPEGs,
yaw-progress.json. Output: body-spin-audit.json in the input directory.
The dated reference breaks belong to this experiment and are not calibration.
OpenCV is used only for image-quality candidates; it never grants clearance.
"""
from pathlib import Path
from datetime import datetime
import hashlib, json, math, argparse
import cv2

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("evidence_root", type=Path, help="Private exported 20261116 spin folders and yaw-progress.json")
parser.add_argument("--output", type=Path, help="Audit output; defaults to evidence_root/body-spin-audit.json")
parser.add_argument("--through", type=int, default=58, help="Last spin number in this frozen archive (default: 58)")
args = parser.parse_args()
root = args.evidence_root.resolve(strict=True)
cv2.setNumThreads(1)
trials = []
totals = dict(planned=0, executed=0, succeeded=0, failed=0, unexecuted=0)
for folder in sorted(root.glob('spin-[0-9][0-9][0-9]')):
    if int(folder.name[5:]) > args.through:
        continue
    path = folder / 'result.json'
    if not path.exists():
        continue
    r = json.loads(path.read_text('utf8'))
    receipt = r.get('agent_receipt') or {}
    rows = [v for v in receipt.get('results', []) if v.get('op') == 'rotate']
    passed = sum(v['status'] == 'done' for v in rows)
    planned = r['requested_pulses']
    assert len(rows) <= planned
    result = dict(tag=folder.name, run_id=receipt.get('run_id'),
                  requested=planned, executed=len(rows), succeeded=passed,
                  failed=len(rows)-passed, unexecuted=planned-len(rows),
                  direction=int(r['requested']), max_raw=r['raw_limit'],
                  pulse_duration=r['pulse_duration'], parked_verified=
                  r.get('health', {}).get('stationary_base_verified', False),
                  result_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                  faults=[])
    for row in rows:
        if row['status'] == 'done':
            continue
        evidence = row.get('rotation', {})
        trace = evidence.get('readbacks', [])
        final = evidence.get('stop_readback', {})
        rebound = {}
        for motor in ('9', '10'):
            if motor in final and trace and motor in trace[0]['wheels']:
                rebound[motor] = (final[motor]['position']-
                    trace[0]['wheels'][motor]['position']+2048) % 4096-2048
        result['faults'].append(dict(step=row['step'],
            wheel_delta_raw=evidence.get('wheel_delta_raw'),
            post_release_delta_raw=rebound, read_glitches=evidence.get('read_glitches'),
            stop_failures=evidence.get('failures')))
    trials.append(result)
    for key in totals:
        totals[key] += result['requested' if key == 'planned' else key]

adjustments = []
for path in sorted(root.glob('adjust-*/result.json')):
    r = json.loads(path.read_text('utf8'))
    for row in (r.get('agent_receipt') or {}).get('results', []):
        if row.get('op') == 'travel':
            adjustments.append(dict(tag=path.parent.name, status=row['status'],
                requested_distance_m=row.get('requested_distance_m'),
                wheel_distance_m_estimate=row.get('wheel_distance_m_estimate'),
                wheel_delta_raw=row.get('wheel_delta_raw')))

trajectory = json.loads((root/'yaw-progress.json').read_text('utf8'))
breaks = {'spin-009/before-cam0.jpg': 'lighting_gap_registration_unknown',
          'spin-025/before-cam0.jpg': 'after_adjust_001_new_station',
          'spin-033/before-cam0.jpg': 'after_adjust_002_new_station',
          'spin-054/before-cam0.jpg': 'after_adjust_003_new_station',
          'spin-056/before-cam0.jpg': 'after_adjust_004_and_low_head_diagnostic_new_reference',
          'spin-059/before-cam0.jpg': 'user_wheel_reinforcement_and_changed_chassis_pose'}
segments = []
current = None

def begin(source, reason):
    value = dict(id=f'body-rgb-reference-{len(segments)+1:02d}',
                 reason=reason, views=[dict(source=source, yaw_deg=0.)],
                 yaw_deg=0., missing_pairs=0)
    segments.append(value)
    return value

for pair in trajectory['pairs']:
    if int(Path(pair['b']).parts[0][5:]) > args.through:
        break
    if current is None:
        current = begin(pair['a'], 'first_body_scan_reference')
    assert current['views'][-1]['source'] == pair['a']
    if pair['b'] in breaks or not pair['ok']:
        current = begin(pair['b'], breaks.get(pair['b'], 'background_match_failed'))
    else:
        current['yaw_deg'] += pair['yaw_deg']
        current['views'].append(dict(source=pair['b'], yaw_deg=current['yaw_deg']))

for segment in segments:
    bins = {}
    for view in segment['views']:
        p = root/view['source']
        gray = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        view['source_sha256'] = hashlib.sha256(p.read_bytes()).hexdigest()
        view['gray_mean'] = float(gray.mean())
        view['sharpness'] = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        view['usable_rgb_candidate'] = view['gray_mean'] >= 20 and view['sharpness'] >= 12
        index = math.floor(view['yaw_deg']/5)
        score = (view['usable_rgb_candidate'], view['sharpness'])
        if index not in bins or score > bins[index][0]:
            bins[index] = (score, view)
    segment['selected_views'] = [v for _, v in sorted(bins.values(), key=lambda v: v[1]['yaw_deg'])]
    segment['frame_count'] = len(segment['views'])
    segment['selected_count'] = len(segment['selected_views'])
    segment['yaw_range_deg_estimate'] = [min(v['yaw_deg'] for v in segment['views']),
                                       max(v['yaw_deg'] for v in segment['views'])]
    segment['heading_basis'] = 'background RGB with approximate optics; uncalibrated independent reference'
    segment['full_body_rotation_verified'] = False
    assert 1 <= segment['selected_count'] <= 100

report = dict(at=datetime.now().astimezone().isoformat(), totals=totals, runs=trials,
    adjustments=adjustments, segments=segments, motion_commands_this_script=0,
    full_body_rotation_verified=False, full_room_map_verified=False,
    narrow_exit_attempted=False, original_faults_retained=True,
    note='Opposite-direction retracing and local adjustments are not new room coverage. '
         'No sum across reference breaks is a valid continuous chassis angle.')
(args.output or root/'body-spin-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
print(json.dumps(dict(totals=totals, adjustments=adjustments,
    segments=[{k:s[k] for k in ('id','reason','frame_count','selected_count','yaw_range_deg_estimate')} for s in segments]), ensure_ascii=False))
