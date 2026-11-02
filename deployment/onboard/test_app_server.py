"""Fake stdio protocol process: interleaving, filtering, reuse and cancellation."""
import json
import sys
import threading
import time

import pytest

from deployment.onboard.app_server import AppServerBrain
from deployment.onboard.brain import TurnFailure
from deployment.onboard.service import Mailbox


ENGINE = r'''
import json,sys,threading,time
lock=threading.Lock()
def send(value):
 with lock: print(json.dumps(value),flush=True)
turns=0
for line in sys.stdin:
 value=json.loads(line);method=value.get('method');identifier=value.get('id');params=value.get('params',{})
 if method=='initialize':send({'id':identifier,'result':{'serverInfo':{'name':'fake'}}})
 elif method in ('thread/start','thread/resume'):send({'id':identifier,'result':{'thread':{'id':params.get('threadId','generated-thread')}}})
 elif method=='turn/start':
  turns+=1;tid='turn-'+str(turns);thread=params['threadId'];text=params['input'][0]['text']
  base={'threadId':thread,'turnId':tid}
  send({'method':'item/started','params':dict(base,item={'id':'hidden','type':'agentMessage','phase':'analysis'})})
  send({'method':'item/agentMessage/delta','params':dict(base,itemId='hidden',delta='PRIVATE_REASONING')})
  send({'method':'item/agentMessage/delta','params':dict(base,itemId='unknown',delta='UNKNOWN_ITEM')})
  send({'id':identifier,'result':{'turn':{'id':tid,'status':'inProgress'}}})
  if 'cancel-fixture' in text:continue
  send({'method':'item/started','params':dict(base,item={'id':'public','type':'agentMessage','phase':'final_answer'})})
  send({'method':'item/agentMessage/delta','params':dict(base,itemId='public',delta='Public reply.' )})
  send({'method':'item/completed','params':dict(base,item={'id':'public','type':'agentMessage','phase':'final_answer','text':'Public reply.'})})
  send({'method':'thread/tokenUsage/updated','params':{'threadId':thread,'tokenUsage':{'last':{'inputTokens':12},'total':{'inputTokens':9999}}}})
  send({'method':'turn/completed','params':{'threadId':thread,'turn':{'id':tid,'status':'failed' if 'fail-fixture' in text else 'completed'}}})
 elif method=='turn/interrupt':
  send({'id':identifier,'result':{}})
  send({'method':'turn/completed','params':{'threadId':params['threadId'],'turn':{'id':params['turnId'],'status':'interrupted'}}})
'''


@pytest.fixture
def brain(tmp_path):
    script = tmp_path/'engine.py'
    script.write_text(ENGINE, encoding='utf-8')
    engine = AppServerBrain('fake', tmp_path, tmp_path, tmp_path/'state', timeout=4)
    engine.server_command = lambda: [sys.executable, str(script)]
    yield engine
    engine.close()


def test_reuses_owned_process_streams_public_items_and_finishes_before_process_exit(brain):
    captured = []
    result = brain.ask('hello', 'one', presentation={'name':'MOSS','persona':'test'}, on_delta=lambda *v: captured.append(v))
    process = brain.process
    assert process.poll() is None
    assert result['text'] == 'Public reply.' and captured
    assert 'PRIVATE' not in json.dumps(captured) and 'UNKNOWN' not in json.dumps(captured)
    assert result['token_usage_last'] == {'inputTokens':12}
    second = brain.ask('hello again','two',presentation={'name':'LUNA','persona':'new role'})
    assert brain.process is process and second['thread_id'] == result['thread_id']
    assert brain.presentation_session_file.exists() and not brain.session_file.exists()


def test_failed_protocol_terminal_is_never_a_success_or_retried(brain):
    with pytest.raises(TurnFailure, match='interrupted or failed'):
        brain.ask('fail-fixture','fail')
    assert brain.process is None


def test_single_turn_cancel_discards_partial_and_mailbox_can_accept_a_new_request(brain,tmp_path):
    mailbox = Mailbox(brain,tmp_path/'mailbox')
    mailbox.submit('cancel-request','cancel-fixture','presence',{'name':'MOSS','persona':'test'})
    threading.Thread(target=mailbox.work,daemon=True).start()
    deadline=time.monotonic()+3
    while not brain.active_turn and time.monotonic()<deadline:time.sleep(.01)
    assert brain.active_turn
    assert mailbox.cancel_request('cancel-request')['status']=='cancelled'
    while mailbox.read('cancel-request')['status']=='running' and time.monotonic()<deadline:time.sleep(.01)
    assert mailbox.read('cancel-request')['status']=='cancelled'
    assert not mailbox.health()['fault']
    assert not brain.cancelled.is_set()
    mailbox.submit('next-request','hello','presence',{'name':'MOSS','persona':'test'})
    while mailbox.read('next-request')['status'] in ('queued','running') and time.monotonic()<deadline:time.sleep(.01)
    assert mailbox.read('next-request')['status']=='completed'


def test_timeout_closes_owned_engine_and_pre_cancel_never_starts(brain):
    brain.timeout=.2
    with pytest.raises(TurnFailure,match='timed out'):
        brain.ask('cancel-fixture','timeout')
    assert brain.process is None and brain.cancelled.is_set()
    with pytest.raises(TurnFailure,match='before it started'):
        brain.ask('hello','no-replay')


def test_public_live_projection_has_no_profile_or_private_context(brain,tmp_path):
    mailbox=Mailbox(brain,tmp_path/'mailbox')
    mailbox.submit('public-id','hello','presence',{'name':'MOSS','persona':'PRIVATE_PERSONA'})
    threading.Thread(target=mailbox.work,daemon=True).start()
    deadline=time.monotonic()+3
    while mailbox.read('public-id')['status']!='completed' and time.monotonic()<deadline:time.sleep(.01)
    value=mailbox.live(0,None)
    assert value['messages'][0]['reply']=='Public reply.'
    assert not any(secret in json.dumps(value) for secret in ('PRIVATE_REASONING','PRIVATE_PERSONA','UNKNOWN_ITEM'))
