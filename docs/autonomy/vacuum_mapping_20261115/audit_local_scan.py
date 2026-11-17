"""Audit exported local jobs and derive head poses from actual step receipts.

Offline only. No controller, camera, network, map writes or motor commands.
Earlier requested-angle atlases remain separate from this corrected derivation.
"""
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path


def audit(folder):
    folder=folder.resolve(strict=True)
    path=folder/'result.json';record=json.loads(path.read_text('utf8'))
    photos=record.get('anchor_photos',[])+record.get('final_photos',[])
    photos += [shot for row in record['observations'] for shot in row['shots']]
    for row in photos:
        source=(folder/row['file']).resolve(strict=True)
        assert source.is_relative_to(folder)
        assert hashlib.sha256(source.read_bytes()).hexdigest()==row['sha256']
    measured=[]
    if record['profile']=='head_layers':
        steps=record['physical_steps']
        for row in record['observations']:
            captured=row['shots'][0]['captured_at']
            available=[step for step in steps if datetime.fromisoformat(step['at']).timestamp()<=captured]
            assert available,'No actual head receipt before source frame'
            actual=max(available,key=lambda step:datetime.fromisoformat(step['at']).timestamp())
            joints=actual['joints'];pan=joints['head:7']['position'];tilt=joints['head:8']['position']
            measured.append(dict(source=row['shots'][0]['file'],source_sha256=row['shots'][0]['sha256'],
                captured_at=captured,joint_readback_at=actual['at'],actual_head_raw=[pan,tilt],
                requested_head_raw=row.get('requested_head_raw',row['head_raw']),
                heading_deg=(pan-2015)*360/4096,pitch_deg=-(tilt-2220)*360/4096,
                calibrated_optical_extrinsics=False))
    rotations=[row for row in record.get('native_receipt',{}).get('results',[]) if row['op']=='rotate']
    translations=[row for row in record.get('native_receipt',{}).get('results',[]) if row['op']=='travel']
    def wheels(state):return {str(m['id']):m['position'] for p in state['ports'] if p['port']=='COM4' for m in p['motors'] if m['id'] in (9,10)}
    before=wheels(record['initial_state']);after=wheels(record['final_state'])
    return dict(id=record['id'],profile=record['profile'],result_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        phase=record['phase'],elapsed_s=record['elapsed_s'],verified_media=len(photos),actual_head_views=measured,
        original_derived_view_metadata_retained=True,registered_pulses=len(rotations),
        passed_pulses=sum(row['status']=='done' for row in rotations),
        registered_translations=len(translations),passed_translations=sum(row['status']=='done' for row in translations),
        registered_wheel_distance_m_estimate=sum(row.get('wheel_distance_m_estimate',0.) for row in translations),
        native_wheel_deltas=[row.get('wheel_delta_raw') for row in rotations],
        wheel_changes_raw={k:(after[k]-before[k]+2048)%4096-2048 for k in before},
        llm_calls=record['llm_calls'],parked_verified=record['parked_verified'],
        phone_samples=record['phone_samples'],phone_epochs=len(record['phone_epochs']),phone_gaps=record['phone_gaps'],
        metric_map_verified=False,origin_return_verified=False,motion_commands_this_script=0)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if args.output.exists():parser.error('Choose a new output; old audits remain unchanged')
    value=audit(args.folder);args.output.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:value[k] for k in ('id','profile','phase','elapsed_s','verified_media','registered_pulses','passed_pulses','phone_samples','wheel_changes_raw')},ensure_ascii=False))


if __name__=='__main__':main()
