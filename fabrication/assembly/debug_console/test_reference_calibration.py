from copy import deepcopy
import json
import pytest
from fabrication.assembly.debug_console.reference_calibration import build_references, read_captures, assert_idle
from fabrication.assembly.debug_console.control_runtime import EXPECTED_SERIALS


@pytest.fixture
def measurements():
    profile={'serials':EXPECTED_SERIALS,'full_manual_range_verified':True,
             'joints':{},'wheels':{'geometry_source':'published_design','verified':False}}
    motors={}; captures={}; ports={'left':'COM4','right':'COM3'}
    for port in ports.values():
        for i in range(1,7):
            key=f'{port}:{i}'
            profile['joints'][key]={'min':1000,'max':3300,'homing_raw':100+i,
                'range_verified':True,'verified':False,'selftest':{'at':'retained'}}
            motors[key]={**profile['joints'][key],'mode':0,'position':2050,'torque':1}
    for side,port in ports.items():
        a={key:2050 for key in motors}
        a.update({f'{port}:2':1700,f'{port}:3':2200,f'{port}:4':2000})
        b={**a,f'{port}:2':2212,f'{port}:3':1688}
        c={**b,f'{port}:4':1488 if side=='left' else 2512}
        for name,positions in [('a',a),('b',b),('c',c),('open',{**c,f'{port}:6':2600}),('closed',{**c,f'{port}:6':1400})]:
            captures[side+'_'+name]={'positions':positions,'port':port,'evidence':{'run_id':'test','step':0}}
    return profile,captures,ports,motors


def test_references_use_physical_buses_and_preserve_ranges_and_evidence(measurements):
    profile,captures,ports,motors=measurements
    original=deepcopy(profile)
    result=build_references(*measurements)
    assert profile==original
    assert result['joints']['COM4:2']['reference_raw']==1700
    assert result['joints']['COM4:2']['direction']==1
    assert result['joints']['COM4:3']['direction']==1
    assert result['joints']['COM4:4']['direction']==1
    assert result['joints']['COM3:4']['direction']==-1
    assert result['joints']['COM4:6']['open_raw']==2592
    assert result['joints']['COM4:6']['closed_raw']==1408
    assert result['wheels']==profile['wheels']
    for key,entry in profile['joints'].items():
        assert all(result['joints'][key][k]==entry[k] for k in ('min','max','homing_raw','selftest'))
    assert sum(bool(j['verified']) for j in result['joints'].values())==8


@pytest.mark.parametrize('field,value',[('homing_raw',999),('min',1001),('max',3299)])
def test_changed_coordinates_are_rejected(measurements,field,value):
    measurements[3]['COM4:2'][field]=value
    with pytest.raises(ValueError,match='坐标或限位'):build_references(*measurements)


@pytest.mark.parametrize('card,joint,value',[('left_b',2,1700),('left_c',3,2200),('right_c',4,2000)])
def test_incomplete_or_inconsistent_abc_is_not_promoted(measurements,card,joint,value):
    port=measurements[1][card]['port']
    measurements[1][card]['positions'][f'{port}:{joint}']=value
    with pytest.raises(ValueError,match='变化不符'):build_references(*measurements)


def test_review_and_real_successful_images_are_required(tmp_path):
    directory=tmp_path/'20261102-120000';directory.mkdir()
    (directory/'shot.jpg').write_bytes(b'image')
    run={'error':None,'results':[{'step':1,'op':'snapshot','status':'done','at':'test',
        'positions':{},'shots':[{'file':'shot.jpg','bytes':5}]}]}
    (directory/'result.json').write_text(json.dumps(run),encoding='utf-8')
    record={'run_id':directory.name,'step':1,'port':'COM4','geometry_reviewed':True,'review_note':'camera checked'}
    manifest={'cards':{'left_a':record}}
    assert read_captures(tmp_path,manifest)['left_a']['port']=='COM4'
    record['geometry_reviewed']=False
    with pytest.raises(ValueError,match='复核'):read_captures(tmp_path,manifest)
    record['geometry_reviewed']=True;run['error']='blocked'
    (directory/'result.json').write_text(json.dumps(run),encoding='utf-8')
    with pytest.raises(ValueError,match='失败'):read_captures(tmp_path,manifest)


def test_active_takeover_or_agent_blocks_reference_apply():
    state={'controller':{'controller':'offline'},'agent_run':{'running':False},
           'limit_selftest':{'running':False},'manual_calibration':{'active':False}}
    assert_idle(state)
    state['agent_run']['running']=True
    with pytest.raises(ValueError,match='正在进行'):assert_idle(state)


@pytest.mark.parametrize('drift',[False,True])
def test_apply_reloads_existing_owner_and_rolls_back_exact_bytes_on_config_drift(measurements,tmp_path,monkeypatch,drift):
    import hashlib
    from fabrication.assembly.debug_console import reference_calibration as mod
    profile,captures,ports,motors=measurements
    for port,i in [('COM3',7),('COM3',8),('COM4',9),('COM4',10)]:
        motors[f'{port}:{i}']={**motors[f'{port}:1'],'id':i}
    for key,motor in motors.items():motor['id']=int(key.split(':')[1])
    monkeypatch.chdir(tmp_path)
    path=tmp_path/'fabrication/assembly/kinematic-calibration.json'
    path.parent.mkdir(parents=True)
    before=json.dumps(profile,indent=3).encode('utf-8')+b'\n'
    path.write_bytes(before)
    manifest={'profile_sha256':hashlib.sha256(before).hexdigest(),'arm_ports':ports,'runs_root':'unused'}
    review=tmp_path/'review.json';review.write_text(json.dumps(manifest),encoding='utf-8')
    calls=[]
    def request(endpoint,body=None):
        calls.append(endpoint)
        if endpoint=='/api/calibration/read':return {}
        reported=deepcopy(motors)
        if drift and path.read_bytes()!=before:reported['COM4:2']['homing_raw']=999
        return {'controller':{'controller':'offline'},'preview_only':False,
            'agent_run':{'running':False},'limit_selftest':{'running':False},
            'manual_calibration':{'active':False,'arm_ports':ports},
            'whole_robot_setup':{'ready':True,'checks':[]},
            'ports':[{'port':port,'connected':True,'motors':[motor for key,motor in reported.items() if key.startswith(port+':')]} for port in ('COM3','COM4')]}
    monkeypatch.setattr(mod,'request',request)
    monkeypatch.setattr(mod,'read_captures',lambda *args:captures)
    monkeypatch.setattr('sys.argv',['references',str(review),'--apply'])
    if drift:
        with pytest.raises(ValueError,match='配置变化'):mod.main()
        assert path.read_bytes()==before
    else:
        mod.main()
        assert json.loads(path.read_bytes())['references_verified']
        assert (tmp_path/'applied-result.json').exists()
    assert (tmp_path/'previous-profile.json').read_bytes()==before
    assert set(calls)=={'/api/original/status','/api/calibration/read'}
