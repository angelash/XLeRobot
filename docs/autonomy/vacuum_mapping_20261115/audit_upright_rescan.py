"""Offline audit of the separately referenced 2026-11-16 upright-phone rescan.

Reads exported evidence only. No HTTP, serial, motion, map writes or inference
of missing steps. All angles remain uncalibrated photographic estimates.
"""
from __future__ import annotations

import argparse
from array import array
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wheels(state):
    return {str(m['id']):m['position'] for p in state.get('ports',[]) if p['port']=='COM4'
            for m in p['motors'] if m['id'] in (9,10)}


def delta(start,end):
    return {key:(end[key]-start[key]+2048)%4096-2048 for key in start if key in end}


def audit_phase(root,pattern):
    runs=[];dimensions=Counter();tokens=set();depth=[];sample_count=rgbd=media_count=0
    unregistered_changes=[]
    for path in sorted(root.glob(pattern+'/result.json')):
        result=json.loads(path.read_text(encoding='utf8'))
        receipt=result.get('agent_receipt') or {}
        steps=[row for row in receipt.get('results',[]) if row.get('op') in ('rotate','travel')]
        planned=result.get('requested_pulses',1)
        runs.append({'tag':path.parent.name,'run_id':receipt.get('run_id'),
                     'planned':planned,'registered':len(steps),
                     'passed':sum(row['status']=='done' for row in steps),
                     'failed':sum(row['status']!='done' for row in steps),
                     'unexecuted_or_unrecorded':planned-len(steps),
                     'error':result.get('error'),'final_error':result.get('final_error'),
                     'result_sha256':sha256(path)})
        if not steps:
            changes=delta(wheels(result.get('initial',{})),wheels(result.get('final_state',{})))
            if any(abs(value)>2 for value in changes.values()):
                unregistered_changes.append({'tag':path.parent.name,'wheel_changes_raw':changes,
                                             'cause':'unconfirmed; not zero motion or a completed step'})
        for photo in result.get('native_photos',[]):
            assert sha256(path.parent/photo['file'])==photo['sha256']
            media_count+=1
        log_path=path.parent/'phone-summary.json'
        if not log_path.exists():continue
        for sample in json.loads(log_path.read_text(encoding='utf8'))['samples']:
            sample_count+=1;tokens.add(tuple(sample['token']))
            meta=sample['meta'];rgbd+=bool(meta.get('rgbd'))
            dimensions[str((meta.get('rgb_width',meta.get('width')),meta.get('rgb_height',meta.get('height'))))]+=1
            for name,entry in sample['files'].items():
                source=path.parent/name
                assert source.stat().st_size==entry['bytes'] and sha256(source)==entry['sha256']
                media_count+=1
                if not name.endswith('depth.f32'):continue
                values=array('f');values.frombytes(source.read_bytes())
                if sys.byteorder!='little':values.byteswap()
                positive=[value for value in values if math.isfinite(value) and value>0]
                depth.append({'positive_median_m':statistics.median(positive) if positive else None,
                              'within_0_2_to_5_m_fraction':sum(.2<=value<=5 for value in values)/len(values)})
    summary={key:sum(row[key] for row in runs) for key in
             ('planned','registered','passed','failed','unexecuted_or_unrecorded')}
    output={'counts':summary,'runs':runs,'phone_samples':sample_count,'unique_phone_tokens':len(tokens),
            'phone_rgb_dimensions':dict(dimensions),'rgbd_samples':rgbd,
            'verified_media':media_count,'unregistered_wheel_changes':unregistered_changes}
    if depth:
        medians=[row['positive_median_m'] for row in depth if row['positive_median_m'] is not None]
        output['depth']={'frames':len(depth),'positive_median_range_m':[min(medians),max(medians)],
                         'within_0_2_to_5_m_fraction_range':[
                             min(row['within_0_2_to_5_m_fraction'] for row in depth),
                             max(row['within_0_2_to_5_m_fraction'] for row in depth)],
                         'source':'ARCore algorithm depth; metric accuracy unverified'}
    yaw_path=root/'yaw-progress.json'
    if yaw_path.exists():
        trajectory=json.loads(yaw_path.read_text(encoding='utf8'))
        angles=[0.]+[row['cumulative_deg'] for row in trajectory['pairs']]
        output['photographic_yaw']={'range_estimate_deg':[min(angles),max(angles)],
                                    'ending_estimate_deg':angles[-1],
                                    'metric_angle_calibrated':False,'other_epochs_added':False}
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ar-root',type=Path,required=True)
    parser.add_argument('--rgb-root',type=Path,required=True)
    parser.add_argument('--control-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    roots=[root.resolve() for root in (args.ar_root,args.rgb_root,args.control_root)]
    if args.output.exists():parser.error('Choose a new output file; prior audits are retained')
    result={'schema_version':1,'motion_commands':0,'phase_epochs_merged':False,
            'ar':audit_phase(roots[0],'spin-*'),'rgb':audit_phase(roots[1],'spin-*'),
            'control':audit_phase(roots[2],'diag-*'),
            'new_full_circle_completed':False,'metric_map_valid':False,
            'scope':'registered steps and hashed media; missing steps remain unknown'}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({key:value['counts'] for key,value in result.items() if isinstance(value,dict) and 'counts' in value},ensure_ascii=False))


if __name__=='__main__':main()
