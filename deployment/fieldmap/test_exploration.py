import pytest
from .exploration import Exploration


def test_start_is_only_a_supervision_record_and_cannot_grant_motion(tmp_path):
    clock=[100.];mode=Exploration(tmp_path,clock=lambda:clock[0]);assert not mode.status()['enabled']
    s=mode.start('user explicitly requested continuing exploration until stop',True)
    assert s['enabled'] and s['supervisor_connected'] and not s['motion_permission']
    clock[0]+=91
    assert not mode.status()['supervisor_connected']
    assert Exploration(tmp_path,clock=lambda:clock[0]).status()['session_id']==s['session_id']


def test_stop_latches_before_both_stop_calls_and_rejects_delayed_update(tmp_path):
    mode=Exploration(tmp_path);s=mode.start('explicit supervised exploration task',True);calls=[]
    def stop(path,body):
        assert not mode.status()['enabled'];calls.append(path)
        if len(calls)==1:raise TimeoutError('controller temporarily unavailable')
    result=mode.stop(stop)
    assert calls==['/api/agent/stop','/api/servo/stop'] and len(result['errors'])==1
    with pytest.raises(ValueError,match='已停止'):mode.update(s['session_id'],'moving','late queued plan')
    assert not Exploration(tmp_path).status()['enabled']


def test_resume_requires_new_explicit_start_and_keeps_old_record(tmp_path):
    mode=Exploration(tmp_path);s=mode.start('explicit supervised exploration task',True)
    mode.stop(lambda *_:None)
    with pytest.raises(ValueError):mode.start('short',False)
    new=mode.start('new explicit supervised exploration task',True)
    assert new['session_id']!=s['session_id'] and list(tmp_path.glob('previous-*.json'))
    with pytest.raises(ValueError):mode.update(s['session_id'],'moving','old session')
    result=mode.update(new['session_id'],'mapping','new observation',translation_legs=2,turn_attempts=1,wheel_path_m=.1)
    assert result['translation_legs']==2 and result['wheel_path_m']==.1


def test_http_stop_and_status_remain_available_when_shared_workers_are_busy(tmp_path):
    import asyncio
    import anyio
    import httpx
    from fastapi import FastAPI
    from .integration import install,API

    async def scenario():
        app=FastAPI();survey=install(app,data_dir=tmp_path);calls=[]
        survey.controller=lambda path,body=None:calls.append(path)
        mode=app.state.exploration
        limiter=anyio.to_thread.current_default_thread_limiter()
        tokens=limiter.total_tokens;limiter.total_tokens=1
        try:
            # Camera and controller sync routes use this same AnyIO limiter.
            async with limiter:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    started=await asyncio.wait_for(client.post(API+'/exploration/start',json={
                        'authorization':'explicit supervised exploration task','confirmed':True}),.5)
                    session=started.json()['session_id']
                    updated=await asyncio.wait_for(client.post(API+'/exploration/update',json={
                        'session_id':session,'phase':'observing','note':'workers busy; no motion'}),.5)
                    assert updated.status_code==200
                    state=await asyncio.wait_for(client.get(API+'/exploration'),.5)
                    assert state.json()['enabled'] and not state.json()['motion_permission']
                    stopped=await asyncio.wait_for(client.post(API+'/exploration/stop',json={}),.5)
                    assert stopped.status_code==200 and not stopped.json()['state']['enabled']
                    assert not mode.status()['enabled']
        finally:limiter.total_tokens=tokens
        assert calls==['/api/agent/stop','/api/servo/stop']

    asyncio.run(scenario())
