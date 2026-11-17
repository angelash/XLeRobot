"""Read-only audit of exported 2026-11-17 bounded control trials.

No serial, network, commands, old-trial relabeling or metric map certification.
Groups actual attempts by controller source SHA and verifies recorded media.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(root):
    trials=[];versions={};seen_runs=set();media=0;phone_samples=0
    phone_tokens=set();phone_sessions=set();phone_trials=[]
    for path in sorted(root.glob('*/result.json')):
        result=json.loads(path.read_text(encoding='utf8'))
        if not path.parent.name.startswith(('diag-','spin-')):continue
        receipt=result.get('agent_receipt') or {};run=receipt.get('run_id')
        if run:
            assert run not in seen_runs,'Duplicate native run';seen_runs.add(run)
        planned=result['requested_pulses'];steps=[row for row in receipt.get('results',[]) if row['op']=='rotate']
        source=result.get('source_sha256',{}).get('rotation_scan.py','not_recorded_before_preflight')
        version=versions.setdefault(source,{'actual':0,'passed':0,'failed':0,'unexecuted_or_unregistered':0,'wheel_deltas':[],
                                           'stop_lock_wait_ms':[],'zero_ack_ms':[],'stop_request_to_zero_ack_ms':[],
                                           'balance_writes':0,'powered_samples':0})
        counts=Counter();counts['planned']=planned;counts['registered']=len(steps)
        for row in steps:
            evidence=row.get('rotation',row);trace=evidence.get('control_trace',[])
            actual=any(e['phase']=='command_ack' and e.get('value',0) for e in trace)
            counts['actual']+=actual
            counts['passed' if row['status']=='done' else 'failed']+=1
            version['actual']+=actual;version['passed' if row['status']=='done' else 'failed']+=1
            deltas=evidence.get('wheel_delta_raw')
            if deltas:version['wheel_deltas'].append({'trial':path.parent.name,'status':row['status'],'deltas':deltas})
            events={e['phase']:e['t_ms'] for e in trace}
            if 'stop_requested' in events and 'stop_io_acquired' in events:
                version['stop_lock_wait_ms'].append(events['stop_io_acquired']-events['stop_requested'])
            version['zero_ack_ms'].extend(e['t_ms'] for e in trace if e['phase']=='zero_ack')
            if 'stop_requested' in events:
                version['stop_request_to_zero_ack_ms'].extend(e['t_ms']-events['stop_requested'] for e in trace if e['phase']=='zero_ack')
            assert all((e.get('value',0)&0x7fff)<=result['raw_limit'] for e in trace if e['phase'] in ('command_ack','balance_ack'))
            assert result['raw_limit']<=160 and result['pulse_duration']<=.65
            version['balance_writes']+=sum(e['phase']=='balance_ack' for e in trace)
            version['powered_samples']+=len(evidence.get('powered_trace',evidence.get('travel_trace',[])))
        counts['unexecuted_or_unregistered']=planned-counts['actual'];version['unexecuted_or_unregistered']+=counts['unexecuted_or_unregistered']
        tail_changes=None
        snapshots=[row for row in receipt.get('results',[]) if row.get('op')=='snapshot' and row.get('wheel_positions')]
        if snapshots:
            anchor=snapshots[-1]['wheel_positions']
            final={f'{p["port"]}:{m["id"]}':m['position'] for p in result.get('final_state',{}).get('ports',[]) for m in p['motors'] if m['id'] in (9,10)}
            if set(anchor)<=set(final):tail_changes={key:(final[key]-anchor[key]+2048)%4096-2048 for key in anchor}
        trials.append({'tag':path.parent.name,'run_id':run,'counts':dict(counts),'result_sha256':sha(path),
                       'error':result.get('error'),'final_error':result.get('final_error'),
                       'parked_verified':result.get('health',{}).get('stationary_base_verified',False),
                       'controller_source_sha256':source,'tail_wheel_changes_raw':tail_changes,
                       'unregistered_tail_motion_possible':bool(tail_changes and any(abs(v)>2 for v in tail_changes.values()))})
        for photo in result.get('native_photos',[]):
            assert sha(path.parent/photo['file'])==photo['sha256'];media+=1
        log=path.parent/'phone-summary.json'
        if log.exists():
            phone=json.loads(log.read_text(encoding='utf8'));tokens=set()
            for sample in phone['samples']:
                token=tuple(sample['token']);assert token not in tokens;tokens.add(token);phone_samples+=1
                phone_tokens.add(token);phone_sessions.add(token[0])
                for name,entry in sample['files'].items():
                    p=path.parent/name;assert p.stat().st_size==entry['bytes'] and sha(p)==entry['sha256'];media+=1
            phone_trials.append({'tag':path.parent.name,'samples':len(phone['samples']),
                'source_errors':len(phone.get('errors',[])),
                'new_reference_required':any('session changed' in row.get('error','').lower() for row in phone.get('errors',[]))})
    for value in versions.values():
        differences=[abs(row['deltas']['9']-row['deltas']['10']) for row in value['wheel_deltas']]
        value['net_wheel_difference_counts']={'median':statistics.median(differences),'max':max(differences)} if differences else None
        waits=value['stop_lock_wait_ms'];value['stop_lock_wait_ms_summary']={'max':max(waits),'median':statistics.median(waits)} if waits else None
        zero=value['stop_request_to_zero_ack_ms'];value['stop_request_to_zero_ack_ms_summary']={'max':max(zero),'median':statistics.median(zero)} if zero else None
    return {'schema_version':1,'scope':'exported finite control trials only','motion_commands_this_script':0,
            'trials':trials,'counts':dict(sum((Counter(row['counts']) for row in trials),Counter())),
            'controller_versions':versions,'verified_media':media,'phone_samples':phone_samples,
            'phone_distinct_source_frames':len(phone_tokens),'phone_recorded_session_count':len(phone_sessions),
            'phone_trials':phone_trials,'phone_sessions_not_silently_merged':True,
            'metric_map_verified':False,'origin_return_verified':False,
            'historical_failures_retained':True,'source_refusals_are_not_physical_failures':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('evidence_root',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('Choose a new output; previous audits are retained')
    result=audit(args.evidence_root.resolve(strict=True));args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:result[k] for k in ('counts','verified_media','phone_samples')},ensure_ascii=False))


if __name__=='__main__':main()
