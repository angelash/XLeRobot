import json
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from fastapi.testclient import TestClient
import pytest

from deployment.onboard.brain import Brain, write_json
from deployment.onboard.persona import OBSERVATION_POLICY
from deployment.onboard.service import Mailbox, handler_for
from presentation.api import create_app


def completed(mailbox, request_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = mailbox.read(request_id)
        if value['status'] == 'completed':
            return value
        time.sleep(.01)
    pytest.fail('Presentation turn did not complete')


def test_saved_display_identity_reaches_mailbox_and_next_turn_without_leaking_context(tmp_path):
    class CharacterBrain:
        cancelled = threading.Event()
        calls = []

        def session(self):
            return {'thread_id': 'existing-conversation'}

        def ask(self, message, request_id, *, presentation):
            self.calls.append((message, presentation))
            return {'text': '我是 ' + presentation['name'], 'thread_id': 'existing-conversation'}

    brain = CharacterBrain()
    mailbox = Mailbox(brain, tmp_path/'mailbox')
    threading.Thread(target=mailbox.work, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(mailbox))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with TestClient(create_app(tmp_path/'presence', onboard_url=f'http://127.0.0.1:{server.server_port}')) as client:
            profile = client.get('/api/presence/config').json()
            for request_id, character in [('moss', {'name': profile['name'], 'persona': profile['persona']}),
                                          ('custom', {'name': 'LUNA', 'persona': '用温暖简短的语气回答。'})]:
                profile.update(character)
                assert client.put('/api/presence/config', json=profile).status_code == 200
                assert client.post('/api/presence/messages', json={'id': request_id, 'message': '你好'}).status_code == 200
                value = completed(mailbox, request_id)
                assert value['message'] == '你好'
                assert value['presentation'] == character
                assert value['reply']['text'] == '我是 ' + character['name']
                assert mailbox.submit(request_id, '你好', 'presence', character) == value
            assert len(brain.calls) == 2
            assert brain.calls[-1] == ('你好', {'name': 'LUNA', 'persona': '用温暖简短的语气回答。'})
            public = mailbox.presentation()
            assert all(row['message'] == '你好' for row in public['messages'])
            assert 'presentation' not in json.dumps(public)
            assert '温暖简短' not in json.dumps(public, ensure_ascii=False)
            with pytest.raises(ValueError, match='different message'):
                mailbox.submit('moss', '你好', 'presence', {'name': 'OTHER', 'persona': ''})
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('context', [[], {}, {'name': '', 'persona': ''}, {'name': 'MOSS', 'persona': 1},
                                  {'name': 'MOSS', 'persona': 'x'*4001},
                                  {'name': 'MOSS', 'persona': '', 'motion_enabled': True}])
def test_invalid_character_never_enters_persistent_queue(tmp_path, context):
    mailbox = Mailbox(object(), tmp_path)
    with pytest.raises(ValueError):
        mailbox.submit('invalid', 'hello', 'presence', context)
    assert mailbox.queue.empty() and not mailbox.path('invalid').exists()


def test_character_is_developer_context_for_new_and_resumed_turns_with_permissions_preserved(tmp_path, monkeypatch):
    capture = tmp_path/'prompt.txt'
    engine = tmp_path/'engine.py'
    engine.write_text(
        'import json,sys\nfrom pathlib import Path\n'
        f'Path({str(capture)!r}).write_bytes(sys.stdin.buffer.read())\n'
        "for event in [{'type':'thread.started','thread_id':'same-conversation'},"
        "{'type':'item.completed','item':{'type':'agent_message','text':'completed'}},"
        "{'type':'turn.completed'}]:print(json.dumps(event))\n", encoding='utf-8')
    calls = []
    original = subprocess.Popen

    def launch(command, **kwargs):
        calls.append(command)
        return original([sys.executable, str(engine)], **kwargs)

    monkeypatch.setattr('deployment.onboard.brain.subprocess.Popen', launch)
    brain = Brain('test-engine', tmp_path/'account', tmp_path, tmp_path/'state')
    write_json(brain.session_file, {'thread_id': 'original-engineering-conversation'})
    original_session = brain.session_file.read_bytes()
    characters = [{'name': 'MOSS', 'persona': '冷静、简洁。'}, {'name': 'LUNA', 'persona': '温暖直接。'}]
    for index, character in enumerate(characters):
        result = brain.ask('你好', f'character-{index}', presentation=character)
        command = calls[-1]
        key, encoded = command[command.index('-c')+1].split('=', 1)
        assert key == 'developer_instructions'
        instructions = json.loads(encoded)
        assert '本轮角色配置由应用' in instructions
        assert OBSERVATION_POLICY in instructions
        assert command[command.index('-s')+1] == 'read-only'
        assert command[command.index('-a')+1] == 'never'
        assert ('resume' in command) == bool(index)
        assert result['thread_id'] == 'same-conversation'
        assert brain.session_file.read_bytes() == original_session
        assert brain.session(presentation=True)['thread_id'] == 'same-conversation'
        prompt = capture.read_text(encoding='utf-8')
        assert prompt.endswith('你好') and '你是车载小九AI' not in prompt
        assert json.dumps(character, ensure_ascii=False) in prompt
    assert not (tmp_path/'account'/'config.toml').exists()
    command = brain.command('same-conversation')
    assert '-c' not in command and 'resume' in command
