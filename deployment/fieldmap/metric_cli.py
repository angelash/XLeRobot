"""Generate or restore an offline mapping archive; never connects to a robot.

python -m deployment.fieldmap.metric_cli demo
python -m deployment.fieldmap.metric_cli replay --input /private/mapping.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from time import perf_counter
import uuid

from .metric_demo import configuration, scans as demo_scans
from .metric_store import MAX_SCANS, MappingStore, canonical


def run(output_root, archive=None):
    started = perf_counter()
    if archive is None:
        config, scans, priors, mode = configuration(), list(demo_scans()), [], 'synthetic_demo'
        provenance = {'kind': 'analytic_synthetic_office'}
    else:
        path = Path(archive)
        if path.stat().st_size > 12_000_000:
            raise ValueError('archive exceeds 12 MB budget')
        raw = path.read_bytes()
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('schema') != 1:
            raise ValueError('expected mapping archive schema 1')
        config = dict(value['config']); mode = config.pop('mode')
        scans, priors = value['scans'], value['external_priors']
        if not isinstance(scans, list) or len(scans) > MAX_SCANS or not isinstance(priors, list) or len(priors) > 10:
            raise ValueError('archive scan/prior budget exceeded')
        provenance = {'kind': 'restored_archive', 'source_sha256': hashlib.sha256(raw).hexdigest(),
                      'source_session_id': value.get('id')}
    # A new output volume even on retry; original files and failed volumes remain.
    folder = Path(output_root).resolve()/uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    store = MappingStore(folder/'database')
    record = store.create(config, mode=mode)
    sid, revision = record['id'], 0
    for scan in scans:
        revision = store.append(sid, revision, scan)['revision']
    for prior in priors:
        revision = store.import_prior(sid, revision, prior)['revision']
    data = store.read(sid)
    preview = store.preview(sid, revision)
    (folder/'mapping.json').write_text(canonical(store.archive(sid))+'\n', encoding='utf-8')
    (folder/'map.pgm').write_bytes(store.pgm(sid))
    grid = data['grid']
    (folder/'map.yaml').write_text(
        f'# Replay only; no navigation clearance\nimage: map.pgm\nresolution: {grid["resolution_m"]}\n'
        f'origin: [{grid["origin_x_m"]}, {grid["origin_y_m"]}, 0.0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n', encoding='utf-8')
    (folder/'map.json').write_text(canonical(data)+'\n', encoding='utf-8')
    (folder/'plan.json').write_text(canonical(preview)+'\n', encoding='utf-8')
    result = {'output': str(folder), 'session_id': sid, 'mode': mode, 'provenance': provenance,
              'scan_count': data['scan_count'], 'counts': data['counts'], 'plan_status': preview['status'],
              'path_nodes': len(preview['path_m']),
              'grid_sha256': hashlib.sha256(json.dumps(data['cells']).encode()).hexdigest(),
              'elapsed_seconds': round(perf_counter()-started, 4), 'llm_calls': 0, 'motion_commands': 0}
    result['files'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir()) if p.is_file()}
    (folder/'receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('demo', 'replay'))
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output-root', type=Path, default=Path('.local-deploy/metric-replays'))
    args = parser.parse_args()
    if args.command == 'replay' and args.input is None:
        parser.error('replay requires --input')
    if args.command == 'demo' and args.input is not None:
        parser.error('demo does not accept --input')
    print(json.dumps(run(args.output_root, args.input), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
