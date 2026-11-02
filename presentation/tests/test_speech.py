from io import BytesIO
import json
import struct
import wave

from fastapi.testclient import TestClient
import httpx
import pytest

from presentation.api import create_app
from presentation.speech import SpeechError,SpeechService,spoken_parts
from presentation.state import Speech


def configured(tmp_path):
    (tmp_path/'speech-provider.json').write_text(json.dumps({'api_key':'fake-secret-test-only',
      'base_url':'https://api.minimax.io/v1/t2a_v2','model':'speech-2.8-turbo'}))


def complete(text='回复文字始终显示。',**patch):
    return {'event_id':'public-1','kind':'message','phase':'completed','title':'完成','text':text,**patch}


def provider(calls):
    def handle(request):
        calls.append(request)
        assert request.headers['Authorization']=='Bearer fake-secret-test-only'
        if request.url.path.endswith('get_voice'):
            return httpx.Response(200,json={'system_voice':[{'voice_id':'Robot_Armor','voice_name':'Robot Armor',
              'description':['An electronic male voice']}], 'voice_cloning':[{'voice_id':'private-test-voice','voice_name':'My voice'}],
              'base_resp':{'status_code':0}})
        assert request.url.path.endswith('t2a_v2')
        value=json.loads(request.content);assert value['stream_options']['exclude_aggregated_audio']
        assert value['audio_setting']=={'sample_rate':16000,'format':'pcm','channel':1}
        content='\n'.join([': heartbeat','data: '+json.dumps({'data':{'audio':struct.pack('<4h',0,1500,-1500,0).hex(),'status':1}}),
                           'data: '+json.dumps({'data':{'audio':'','status':2},'base_resp':{'status_code':0}}),'data: [DONE]'])
        return httpx.Response(200,text=content)
    return httpx.MockTransport(handle)


def test_disabled_by_default_preserves_text_and_makes_no_provider_call(tmp_path):
    configured(tmp_path);app=create_app(tmp_path);calls=[]
    with TestClient(app) as client:
        app.state.presence.speech_service.transport=provider(calls)
        profile=client.get('/api/presence/config').json()
        assert profile['speech']['enabled'] is False
        event=client.post('/api/presence/events',json=complete(),headers={'X-Presence-Producer':'1'}).json()
        response=client.post('/api/presence/speech',json={'epoch':event['epoch'],'event_id':event['event_id']})
        assert response.status_code==409 and not calls
        assert client.get('/api/presence/state').json()['events'][-1]['text']=='回复文字始终显示。'
        assert 'fake-secret' not in client.get('/api/presence/speech/status').text
        response=client.post('/api/presence/speech/preview',json={'voice_id':'Robot_Armor','text':'你好'},headers={'Origin':'https://foreign.example'})
        assert response.status_code==403 and not calls


def test_preview_and_all_voice_types_do_not_enable_speech_or_leak_credentials(tmp_path):
    configured(tmp_path);app=create_app(tmp_path);calls=[]
    with TestClient(app) as client:
        app.state.presence.speech_service.transport=provider(calls)
        catalogue=client.get('/api/presence/voices').json()
        assert catalogue['total']==2 and catalogue['source']=='provider'
        assert {row['category'] for row in catalogue['voices']}=={'system_voice','voice_cloning'}
        assert 'fake-secret' not in json.dumps(catalogue)
        response=client.post('/api/presence/speech/preview',json={'voice_id':'Robot_Armor','text':'你好'})
        assert response.status_code==200 and response.headers['content-type']=='audio/wav'
        with wave.open(BytesIO(response.content)) as audio:
            assert audio.getframerate()==16000 and audio.getnchannels()==1 and audio.getnframes()==4
        again=client.post('/api/presence/speech/preview',json={'voice_id':'Robot_Armor','text':'你好'})
        assert again.headers['X-Speech-Cached']=='true' and len(calls)==2
        assert client.get('/api/presence/config').json()['speech']['enabled'] is False


def test_speech_only_reads_bound_public_reply_with_split_and_epoch_checks(tmp_path):
    configured(tmp_path);app=create_app(tmp_path);calls=[]
    with TestClient(app) as client:
        app.state.presence.speech_service.transport=provider(calls)
        profile=client.get('/api/presence/config').json();profile['speech']['enabled']=True
        assert client.put('/api/presence/config',json=profile).status_code==200
        event=client.post('/api/presence/events',json=complete('测试。'*150),headers={'X-Presence-Producer':'1'}).json()
        payload={'epoch':event['epoch'],'event_id':event['event_id'],'part':0}
        response=client.post('/api/presence/speech',json=payload)
        assert response.status_code==200 and int(response.headers['X-Speech-Parts'])>1
        assert len(json.loads(calls[-1].content)['text'])<=220
        assert client.post('/api/presence/speech',json={**payload,'epoch':'old'}).status_code==410
        tool=client.post('/api/presence/events',json=complete(event_id='tool',kind='tool'),headers={'X-Presence-Producer':'1'}).json()
        assert client.post('/api/presence/speech',json={'epoch':tool['epoch'],'event_id':'tool'}).status_code==422
        demo=client.post('/api/presence/events',json=complete(event_id='demo',evidence='demo'),headers={'X-Presence-Producer':'1'}).json()
        assert client.post('/api/presence/speech',json={'epoch':demo['epoch'],'event_id':'demo'}).status_code==422


def test_provider_error_keeps_text_and_never_returns_credentials(tmp_path):
    configured(tmp_path);app=create_app(tmp_path)
    with TestClient(app) as client:
        app.state.presence.speech_service.transport=httpx.MockTransport(lambda r:httpx.Response(200,text='data: '+json.dumps({'base_resp':{'status_code':2049,'status_msg':'fake-secret-test-only'}})))
        response=client.post('/api/presence/speech/preview',json={'voice_id':'Robot_Armor','text':'你好'})
        assert response.status_code==503 and '2049' in response.text and 'fake-secret' not in response.text
        assert not list((tmp_path/'speech-cache').glob('*.wav'))


def test_old_profile_defaults_off_without_rewriting_original(tmp_path):
    from presentation.state import Profile
    value=Profile().model_dump();value.pop('speech')
    path=tmp_path/'profile.json';original=json.dumps(value);path.write_text(original)
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/presence/config').json()['speech']['enabled'] is False
    assert path.read_text()==original


@pytest.mark.parametrize('audio,status',[('0100',1),('abc',2),('zz',2)])
def test_incomplete_and_malformed_audio_is_rejected(tmp_path,audio,status):
    import asyncio
    configured(tmp_path);service=SpeechService(tmp_path,transport=httpx.MockTransport(lambda r:httpx.Response(200,text='data: '+json.dumps({'data':{'audio':audio,'status':status}}))))
    with pytest.raises(SpeechError):asyncio.run(service.synthesize('你好',Speech(voice_id='Robot_Armor')))


def test_spoken_parts_do_not_read_code_and_keep_all_prose():
    assert spoken_parts('前面。\n```python\nprint(1)\n```\n[后面](https://example.com)。')==['前面。 后面。']
    long='甲'*500
    assert ''.join(spoken_parts(long))==long and max(map(len,spoken_parts(long)))<=220


def test_streamed_public_sentences_are_bound_to_generation_and_cancel_invalidates_audio(tmp_path):
    configured(tmp_path);app=create_app(tmp_path);calls=[]
    with TestClient(app) as client:
        store=app.state.presence
        store.speech_service.transport=provider(calls)
        profile=client.get('/api/presence/config').json();profile['speech']['enabled']=True
        client.put('/api/presence/config',json=profile)
        reply=store.replies.update('generated-stream','第一句。未完成部分')
        body={'request_id':'generated-stream','generation_id':reply['generation_id'],'part':0}
        assert client.post('/api/presence/speech/reply',json=body).status_code==200
        assert json.loads(calls[-1].content)['text']=='第一句。'
        assert client.post('/api/presence/speech/reply',json={**body,'part':1}).status_code==422
        store.replies.update('generated-stream',reply['text'],status='cancelled')
        assert client.post('/api/presence/speech/reply',json=body).status_code==410
