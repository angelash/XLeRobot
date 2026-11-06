import json

from deployment.grasp.practice import counts, load_trial


def test_search_refusals_and_other_arm_never_inflate_actual_closing_count():
    rows=[{'side':'left','ok':False,'entered_closure':False},
          {'side':'left','ok':False,'entered_closure':True},
          {'side':'left','ok':True,'entered_closure':True},
          {'side':'right','ok':True,'entered_closure':True}]
    result=counts(rows,'left',100)
    assert (result['requests'],result['actual_closures'],result['local_passes'])==(3,2,1)
    assert result['remaining_closures']==98 and not result['count_target_reached']


def test_count_actual_close_from_complete_events_even_when_status_tail_lost_it(tmp_path):
    result={'side':'left','ok':False,'started_at':'2026-11-06T17:00:00+08:00',
            'controller':{'events':[{'phase':'failed'}]}}
    (tmp_path/'result.json').write_text(json.dumps(result),encoding='utf8')
    (tmp_path/'events.jsonl').write_text(json.dumps({'phase':'close','kind':'step','targets':{'left:6':1960}})+'\n',encoding='utf8')
    row=load_trial(tmp_path/'result.json')
    assert row['entered_closure'] and not row['ok']


def test_close_phase_without_an_executed_jaw_step_does_not_count(tmp_path):
    (tmp_path/'result.json').write_text(json.dumps({'side':'left','ok':False,
        'started_at':'2026-11-06T17:00:00+08:00'}),encoding='utf8')
    (tmp_path/'events.jsonl').write_text(json.dumps({'phase':'close','kind':'phase'})+'\n',encoding='utf8')
    assert not load_trial(tmp_path/'result.json')['entered_closure']


def test_failed_request_ends_epoch_without_an_automatic_second_grasp(tmp_path,monkeypatch):
    from deployment.grasp import practice
    from types import SimpleNamespace
    import sys
    root=tmp_path/'practice'
    calls=[]
    def run(command,**kwargs):
        calls.append(command)
        dest=practice.Path(command[command.index('--root')+1]);dest.mkdir(parents=True)
        (dest/'result.json').write_text(json.dumps({'action':'pick','side':'left','ok':False,
            'started_at':'2026-11-06T17:00:00+08:00','error':'uncertain target'}),encoding='utf8')
        return SimpleNamespace(stdout='',stderr='')
    monkeypatch.setattr(practice.subprocess,'run',run)
    monkeypatch.setattr(sys,'argv',['practice','--root',str(root),'--side','left',
                                  '--appearance','dark','--epoch','left-test','--limit','100','--confirmed'])
    practice.main()
    assert len(calls)==1
    assert calls[0][calls[0].index('--side')+1]=='left'
    assert calls[0][calls[0].index('--appearance')+1]=='dark'
    saved=json.loads((root/'left-progress.json').read_text('utf8'))
    assert saved['counts']['actual_closures']==0 and saved['counts']['requests']==1
    assert saved['status']=='ended' and not (root/'practice.lock').exists()
    assert saved['protocol']['appearance']=='dark'


def test_persistent_arm_hold_prevents_new_trials_after_physical_failures(tmp_path,monkeypatch):
    from deployment.grasp import practice
    import sys
    root=tmp_path/'practice';root.mkdir()
    (root/'ARM_HOLDS.json').write_text(json.dumps({'right':'shoulder timeout then contact; inspect'}),encoding='utf8')
    def forbidden(*a,**kw):raise AssertionError('held arm must not run a subprocess')
    monkeypatch.setattr(practice.subprocess,'run',forbidden)
    monkeypatch.setattr(sys,'argv',['practice','--root',str(root),'--side','right',
                                  '--epoch','right-hold','--confirmed'])
    practice.main()
    saved=json.loads((root/'right-progress.json').read_text('utf8'))
    assert saved['counts']['requests']==0 and saved['reason'].startswith('arm held')
    assert not (root/'practice.lock').exists()
