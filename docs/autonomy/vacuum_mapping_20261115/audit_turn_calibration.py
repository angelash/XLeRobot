"""Read-only recheck of a private turn-calibration evidence ZIP; no robot I/O.

From repository root:
  python docs/autonomy/vacuum_mapping_20261115/audit_turn_calibration.py evidence.zip
"""
from __future__ import annotations
import argparse,hashlib,json,sys,zipfile
from pathlib import Path
import cv2
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from deployment.fieldmap.visual_turn import rgb_turn,fit_turn_gain,color_intrinsics


def audit(path):
    with zipfile.ZipFile(path) as z:
        manifest=json.loads(z.read('manifest.json'))
        assert z.testzip() is None
        for row in manifest['files']:
            raw=z.read(row['name'])
            assert len(raw)==row['bytes'] and hashlib.sha256(raw).hexdigest()==row['sha256']
        snapshot=json.loads(z.read('maintenance/archive-snapshot-01.json'))
        parents=[];native=[];heads=[]
        for name in z.namelist():
            if not (name.startswith('fieldmap/turn-calibration/') and name.count('/')==3 and name.endswith('/result.json')):continue
            parent=json.loads(z.read(name));trials=[]
            prefix=name.rsplit('/',1)[0]
            for i,row in enumerate(parent.get('trials',[])):
                folder=prefix+'/trials/'+f'{i:03d}'+'/input/'
                frames=json.loads(z.read(folder+'frames.json'))['frames']
                fractions=[]
                for frame in frames:
                    rgb=z.read(folder+frame['rgb_file'])
                    assert hashlib.sha256(rgb).hexdigest()==frame['rgb_sha256']
                    if not frame.get('depth_file'):continue
                    depth=z.read(folder+frame['depth_file'])
                    assert hashlib.sha256(depth).hexdigest()==frame['depth_sha256']
                    values=np.frombuffer(depth,dtype='<f4')
                    assert len(values)==frame['meta']['width']*frame['meta']['height']
                    fractions.append(float(((values>=.2)&(values<=3)&np.isfinite(values)).mean()))
                a,b=frames[0],frames[-1]
                decode=lambda f:cv2.imdecode(np.frombuffer(z.read(folder+f['rgb_file']),np.uint8),cv2.IMREAD_GRAYSCALE)
                visual=rgb_turn(decode(a),decode(b),a.get('rgb_intrinsics') or color_intrinsics(a['meta'])[2])
                trials.append(dict(index=i,frames=len(frames),design_angle_deg=row['design_angle_deg'],
                    visual=visual,depth_02_to_3m_fraction_range=[min(fractions),max(fractions)] if fractions else None))
            checked=fit_turn_gain(parent.get('trials',[])[:6],parent.get('trials',[])[6:])
            parents.append(dict(id=parent['id'],phase=parent['phase'],hard_hold=parent.get('hard_hold'),
                completed_trials=trials,fit_accepted=checked['accepted'],fit_reason=checked['reason']))
        for name in z.namelist():
            if not (name.startswith('fieldmap/local-scan/') and name.count('/')==3 and name.endswith('/result.json')):continue
            child=json.loads(z.read(name));rows=[r for r in child.get('native_receipt',{}).get('results',[]) if r.get('op')=='rotate']
            if child['profile']=='head_layers':
                heads.append(dict(id=child['id'],phase=child['phase'],views=child['completed_views'],
                    physical_steps=len(child['physical_steps']),phone_samples=child['phone_samples'],
                    phone_gaps=child['phone_gaps'],elapsed_s=child['elapsed_s'],parked_verified=child['parked_verified']))
                continue
            final={str(m['id']):m['position'] for p in child['final_state']['ports'] if p['port']=='COM4' for m in p['motors'] if m['id'] in (9,10)}
            initial={str(m['id']):m['position'] for p in child['initial_state']['ports'] if p['port']=='COM4' for m in p['motors'] if m['id'] in (9,10)}
            last=rows[-1]['stop_readback'] if rows else {}
            tail={k:(final[k]-last[k]['position']+2048)%4096-2048 for k in final if k in last}
            native.append(dict(child_id=child['id'],native_run_id=child.get('native_run_id'),
                phase=child['phase'],error=child.get('error'),planned_pulses=child['planned_pulses'],
                registered_pulses=len(rows),passed_pulses=sum(r['status']=='done' for r in rows),
                encoder_change_after_last_registered_stop=tail,
                net_encoder_change_raw={k:(final[k]-initial[k]+2048)%4096-2048 for k in final},
                wrapper_native_running=child.get('native_receipt',{}).get('running'),
                unregistered_tail_not_a_completed_action=child['phase']!='completed',
                parked_verified=child['parked_verified'],elapsed_s=child['elapsed_s']))
        return dict(archive_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            payloads=len(manifest['files']),crc_and_member_sha256_verified=True,cutoff=manifest['cutoff'],
            experiments=parents,native_children=native,head_surveys=heads,
            cutoff_status=snapshot['calibration'],cutoff_health=snapshot['health'],
            map_sha256=snapshot['original_map_sha256'],protected_sha256=snapshot['protected_sha256'],
            mission=snapshot['mission'],actuator_commands=0,geometry_parameters_written=False,
            metric_accuracy_verified=False,autonomous_navigation_verified=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('archive');args=parser.parse_args()
    print(json.dumps(audit(args.archive),ensure_ascii=False,indent=2,allow_nan=False))
