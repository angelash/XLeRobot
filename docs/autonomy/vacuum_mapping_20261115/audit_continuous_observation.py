"""Read-only verification of a private continuous-observation evidence archive."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def audit(path):
    with zipfile.ZipFile(path) as z:
        manifest=json.loads(z.read('manifest.json'))
        assert z.testzip() is None
        for row in manifest['files']:
            raw=z.read(row['name'])
            assert len(raw)==row['bytes'] and hashlib.sha256(raw).hexdigest()==row['sha256']
        snapshot_name='maintenance/archive-snapshot-02.json' if 'maintenance/archive-snapshot-02.json' in z.namelist() else 'maintenance/archive-snapshot-01.json'
        snapshot=json.loads(z.read(snapshot_name))
        jobs=[]
        for name in z.namelist():
            if not (name.startswith('fieldmap/autonomy/') and name.count('/')==3 and name.endswith('/result.json')):continue
            parent=json.loads(z.read(name));child_id=parent['completed_child_id']
            child=json.loads(z.read('fieldmap/local-scan/'+child_id+'/result.json'))
            assert child['profile']=='head_layers' and child['phase']=='completed' and child['parked_verified']
            positions=lambda state:{str(m['id']):m['position'] for p in state['ports'] if p['port']=='COM4' for m in p['motors'] if m['id'] in (9,10)}
            initial,final=positions(child['initial_state']),positions(child['final_state'])
            prefix=name.rsplit('/',1)[0]
            events=[json.loads(line) for line in z.read(prefix+'/events.jsonl').decode('utf8').splitlines() if line]
            assert all(a['sequence']<b['sequence'] for a,b in zip(events,events[1:]))
            starts=[r for r in events if r['kind']=='local_action_selected']
            rounds=[r for r in events if r['kind']=='round_completed']
            observations=[r for r in events if r['kind']=='parked_observation_saved']
            for item in observations:
                record=json.loads(z.read(prefix+'/observations/'+item['observation_id']+'/record.json'))
                assert record['motor_commands']==0 and record['adds_explored_area'] is False
                for photo in record['photos']:
                    raw=z.read(prefix+'/observations/'+item['observation_id']+'/'+photo['file'])
                    assert len(raw)==photo['bytes'] and hashlib.sha256(raw).hexdigest()==photo['sha256']
            jobs.append(dict(id=parent['id'],continuous=parent.get('continuous',False),phase=parent['phase'],
                running=parent['running'],elapsed_s=parent['elapsed_s'],round=parent.get('round'),
                hard_hold=parent.get('hard_hold'),finished_at=parent.get('finished_at'),
                interventions=[r for r in parent.get('events',[]) if r['kind']=='intervention_required'],
                rounds_completed=len(rounds),station_actions_selected=len(starts),
                parked_observations=len(observations),child_id=child_id,views=child['completed_views'],
                head_steps=len(child['physical_steps']),phone_samples=child['phone_samples'],phone_gaps=child['phone_gaps'],
                child_elapsed_s=child['elapsed_s'],atlas_id=child.get('atlas_id'),
                wheel_net_raw={k:(final[k]-initial[k]+2048)%4096-2048 for k in final}))
        ongoing=[r for r in jobs if r['continuous']]
        assert len(ongoing)==1 and ongoing[0]['round']>=2 and ongoing[0]['station_actions_selected']==1
        diagnostic_name='maintenance/reference-diagnostic-01.json'
        diagnostic=None
        if diagnostic_name in z.namelist():
            source=json.loads(z.read(diagnostic_name))
            assert source['motion_commands']==0 and source['stop_latch_preserved'] and source['reason_unknown']
            assert len(source['samples'])==3
            for sample in source['samples']:
                assert sample['probe']['motion_commands']==0 and sample['probe']['configuration_writes']==0
                assert all(row['ok'] for row in sample['probe']['motors'])
            diagnostic=dict(samples=[r['delta_from_child_final'] for r in source['samples']],
                health=source['health'],cause='unknown',motion_commands=0,stop_latch_preserved=True)
        return dict(archive_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),payloads=len(manifest['files']),
            member_sha256_length_and_crc_verified=True,cutoff=manifest['cutoff'],ongoing_task_snapshot=manifest['ongoing_task_snapshot'],jobs=jobs,
            final_health=snapshot['health'],controller_pid=snapshot['state']['server_pid'],
            final_wheels=positions(snapshot['state']),map_sha256=snapshot['original_map_sha256'],
            protected_sha256=snapshot['protected_sha256'],final_autonomy=snapshot['autonomy'],
            phone=snapshot['phone']['devices']['android'],
            cumulative_mission={k:snapshot['mission'][k] for k in ('translation_legs','turn_attempts','wheel_path_m')},
            diagnostic=diagnostic,base_commands_this_turn=snapshot['motion_commands'],actual_roaming=False,automatic_navigation=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('archive');args=parser.parse_args()
    print(json.dumps(audit(args.archive),ensure_ascii=False,indent=2,allow_nan=False))
