"""Finite local grasp practice with persistent counts and immutable epochs.

No scheduler, model calls, serial access, automatic recovery or blind retry.
Each unsuccessful request ends the epoch for inspection. The target counts
actual closing phases, not camera searches or skipped plans.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

DEPENDENCIES = ('practice.py', 'local_trials.py', 'flow.py', 'controller.py',
                'tracker.py', 'arm.py', 'vision.py', 'service.py', 'recovery.py')


def load_trial(path):
    result = json.loads(path.read_text(encoding='utf8'))
    event_path = path.with_name('events.jsonl')
    events = ([json.loads(line) for line in event_path.read_text(encoding='utf8').splitlines() if line]
              if event_path.exists() else result.get('controller', {}).get('events', []))
    closed = any(e.get('phase') == 'close' and e.get('kind') == 'step'
                 and any(k.endswith(':6') for k in e.get('targets', {})) for e in events)
    return {'tag': path.parent.name, 'side': result['side'], 'ok': result.get('ok') is True,
            'appearance': result.get('appearance', 'light' if result['side']=='left' else 'dark'),
            'experimental_grip_floor': result.get('experimental_grip_floor', 1450),
            'entered_closure': closed, 'error': result.get('error'), 'contact': result.get('contact'),
            'started_at': result['started_at'], 'finished_at': result.get('finished_at'),
            'seconds': result.get('seconds'), 'policy_sha256': result.get('policy_sha256'),
            'llm_calls': result.get('llm_calls'), 'receipt': str(path)}


def counts(rows, side, target):
    selected = [r for r in rows if r['side'] == side]
    closures = sum(r['entered_closure'] for r in selected)
    return {'side': side, 'target': target, 'requests': len(selected), 'actual_closures': closures,
            'local_passes': sum(r['ok'] for r in selected),
            'preclosure_refusals': sum(not r['entered_closure'] for r in selected),
            'remaining_closures': max(0, target-closures), 'count_target_reached': closures >= target,
            'truth': 'local observations only; not independent physical success labels'}


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--epoch', required=True)
    parser.add_argument('--side', choices=('left', 'right'), required=True)
    parser.add_argument('--appearance', choices=('light', 'dark'))
    parser.add_argument('--grip-floor', type=int, default=1450)
    parser.add_argument('--target', type=int, default=100)
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--confirmed', action='store_true')
    args = parser.parse_args()
    appearance = args.appearance or ('light' if args.side=='left' else 'dark')
    from .local_trials import validated_grip_floor
    try:
        floor = validated_grip_floor(args.side, appearance, args.grip_floor)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.confirmed or not 1 <= args.target <= 100 or not 1 <= args.limit <= 100:
        parser.error('Confirmed bounded workspace and counts 1..100 are required')
    if not args.epoch.isascii() or not args.epoch.replace('-', '').isalnum():
        parser.error('Epoch must be ASCII alphanumeric or hyphens')
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    epoch = root/'epochs'/args.epoch
    epoch.mkdir(parents=True, exist_ok=False)
    lock = root/'practice.lock'
    # A stale lock is deliberately not removed automatically after a crash.
    lock_handle = lock.open('x', encoding='utf8')
    lock_handle.write(json.dumps({'pid':os.getpid(), 'epoch':args.epoch, 'side':args.side}))
    lock_handle.close()
    source = Path(__file__).parent
    frozen = {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in DEPENDENCIES}
    for name in frozen:
        (epoch/name).write_bytes((source/name).read_bytes())
    protocol = {'side':args.side, 'appearance':appearance, 'experimental_grip_floor':floor,
                'target_closures':args.target, 'request_limit':args.limit,
                'dependencies':frozen, 'started_at':datetime.now().astimezone().isoformat(),
                'count_rule':'every actual close phase counts once, all failures retained',
                'stop_rule':'first unsuccessful request, changed code, timeout, stop marker, or hardware guard',
                'llm_calls':0, 'no_automatic_recovery':True}
    atomic_json(epoch/'protocol.json', protocol)
    paths = sorted(set(root.glob('trials/*/result.json')) | set(root.glob('*/result.json')))
    rows = [load_trial(p) for p in paths if json.loads(p.read_text(encoding='utf8')).get('action')=='pick']
    started = time.monotonic()
    reason = 'request limit'
    try:
        for n in range(args.limit):
            holds = root/'ARM_HOLDS.json'
            if holds.exists():
                held = json.loads(holds.read_text(encoding='utf8')).get(args.side)
                if held:
                    reason = 'arm held for inspection: '+str(held)
                    break
            if counts(rows, args.side, args.target)['count_target_reached']:
                reason = 'count target reached'; break
            if (root/'STOP').exists():
                reason = 'stop marker'; break
            if any(hashlib.sha256((source/name).read_bytes()).hexdigest() != digest for name,digest in frozen.items()):
                reason = 'policy dependency changed'; break
            tag = f'{args.epoch}-{n+1:03d}'
            dest = root/'trials'/tag
            dest.parent.mkdir(exist_ok=True)
            began = time.monotonic()
            try:
                completed = subprocess.run([sys.executable, '-X', 'utf8', '-m',
                    'deployment.grasp.local_trials', 'pick', '--side', args.side,
                    '--appearance', appearance,
                    '--grip-floor', str(floor),
                    '--root', str(dest), '--confirmed'], cwd=source.parent.parent,
                    capture_output=True, text=True, encoding='utf8', timeout=290)
            except subprocess.TimeoutExpired:
                reason = 'trial process timeout; inspect current pose and expired lease before resuming'
                break
            (epoch/f'{tag}-stdout.txt').write_text(completed.stdout+completed.stderr, encoding='utf8')
            if not (dest/'result.json').exists():
                reason = 'trial process failed without receipt'; break
            row = load_trial(dest/'result.json')
            row['wall_seconds'] = round(time.monotonic()-began, 2)
            rows.append(row)
            report = {'protocol':protocol, 'elapsed_s':round(time.monotonic()-started, 1),
                      'counts':counts(rows, args.side, args.target), 'rows':rows,
                      'last':row, 'status':'running'}
            atomic_json(epoch/'summary.json', report)
            atomic_json(root/f'{args.side}-progress.json', report)
            print(json.dumps({'counts':report['counts'], 'last':row}, ensure_ascii=False), flush=True)
            if not row['ok']:
                reason = 'unsuccessful request; inspect evidence before next epoch'; break
            # The arm is open at its high observation pose after success.
            # This short pause does not hold an expiring servo lease.
            time.sleep(2)
        report = {'protocol':protocol, 'elapsed_s':round(time.monotonic()-started, 1),
                  'counts':counts(rows, args.side, args.target), 'rows':rows,
                  'status':'ended', 'reason':reason}
        atomic_json(epoch/'summary.json', report)
        atomic_json(root/f'{args.side}-progress.json', report)
        print(json.dumps({'counts':report['counts'], 'reason':reason}, ensure_ascii=False), flush=True)
    finally:
        lock.unlink()


if __name__ == '__main__':
    main()
