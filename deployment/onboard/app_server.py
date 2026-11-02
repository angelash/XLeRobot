"""Owned stdio engine. Turns end on protocol completion, not process exit.

Protocol pinned by the installed engine's generated v2 schemas. No HTTP port,
credential extraction, automatic retry or transport fallback is involved.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time

from .brain import Brain, POLICY, TurnFailure, write_json
from .persona import character_instructions, character_prompt, validate_presentation


PUBLIC_PHASES = {None, 'final_answer'}


class AppServerBrain(Brain):
    supports_stream = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.notifications = queue.Queue(maxsize=4096)
        self.responses = {}
        self.rpc_guard = threading.RLock()
        self.send_guard = threading.Lock()
        self.sequence = 0
        self.active_turn = None
        self.loaded = set()
        self.transport_epoch = 0
        self.last_error = None

    def server_command(self):
        return [self.executable, '-a', 'never', '-s', 'read-only', '-C', self.workspace,
                'app-server', '--stdio']

    def _send(self, value):
        with self.send_guard:
            if not self.process or self.process.poll() is not None:
                raise TurnFailure('Owned engine is unavailable; request was not replayed')
            try:
                self.process.stdin.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                raise TurnFailure('Owned engine disconnected; request was not replayed') from None

    def _rpc(self, method, params, timeout=20):
        with self.rpc_guard:
            self.sequence += 1
            identifier = self.sequence
            response = queue.Queue(maxsize=1)
            self.responses[identifier] = response
        try:
            self._send({'id': identifier, 'method': method, 'params': params})
            try:
                value = response.get(timeout=max(.05, timeout))
            except queue.Empty:
                raise TurnFailure('Engine protocol timed out; request was not replayed') from None
            if 'error' in value:
                # Do not expose arbitrary engine error bodies through public HTTP.
                raise TurnFailure('Engine rejected the protocol request; inspect private diagnostics')
            return value.get('result', {})
        finally:
            with self.rpc_guard:
                self.responses.pop(identifier, None)

    def _read(self, process, epoch):
        try:
            for raw in iter(process.stdout.readline, b''):
                if len(raw) > 2 * 1024**2:
                    raise ValueError('oversized engine event')
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if epoch != self.transport_epoch:
                    return
                if 'id' in event and 'method' not in event:
                    with self.rpc_guard:
                        target = self.responses.get(event['id'])
                    if target:
                        try: target.put_nowait(event)
                        except queue.Full: pass
                elif 'id' in event:
                    # read-only/never must not become an interactive approval path.
                    self._send({'id': event['id'], 'error': {'code': -32601, 'message': 'Interactive requests are disabled'}})
                else:
                    try: self.notifications.put_nowait(event)
                    except queue.Full:
                        self.last_error = 'Engine notification buffer exceeded its limit'
                        process.kill()
                        break
        except (OSError, ValueError):
            self.last_error = 'Engine event stream failed'
        finally:
            if epoch == self.transport_epoch:
                with self.rpc_guard:
                    for target in self.responses.values():
                        try: target.put_nowait({'error': {'message': 'engine exited'}})
                        except queue.Full: pass
                try: self.notifications.put_nowait({'method': '_closed'})
                except queue.Full: pass

    @staticmethod
    def _drain_stderr(process):
        # Pipe is always drained; raw account/runtime details stay private.
        while process.stderr.read(8192):
            pass

    def _ensure_server(self):
        if self.process and self.process.poll() is None:
            return
        if self.cancelled.is_set():
            raise TurnFailure('Turn was cancelled before it started')
        self.transport_epoch += 1
        self.loaded.clear()
        self.notifications = queue.Queue(maxsize=4096)
        env = os.environ.copy()
        env.update(CODEX_HOME=self.code_home, PYTHONIOENCODING='utf-8')
        self.process = subprocess.Popen(self.server_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, cwd=self.workspace, env=env,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        threading.Thread(target=self._read, args=(self.process, self.transport_epoch), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self.process,), daemon=True).start()
        self._rpc('initialize', {'clientInfo': {'name': 'xlerobot-onboard', 'version': '2.0.0'}})
        self._send({'method': 'initialized', 'params': {}})

    def cancel(self):
        self.cancelled.set()
        active = self.active_turn
        if active and self.process and self.process.poll() is None:
            try:
                self._rpc('turn/interrupt', {'threadId': active[0], 'turnId': active[1]}, timeout=3)
            except TurnFailure:
                self.close()

    def close(self):
        process = self.process
        if not process:
            return
        try:
            process.stdin.close()
            process.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                                   capture_output=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    process.kill()
                process.wait(timeout=3)
        self.process = None
        self.active_turn = None

    def ask(self, message, request_id, *, presentation=None, on_delta=None):
        presentation = validate_presentation(presentation)
        if self.cancelled.is_set():
            raise TurnFailure('Turn was cancelled before it started')
        if not self.lock.acquire(blocking=False):
            raise TurnFailure('Another turn is running')
        try:
            return self._turn(message, request_id, presentation, on_delta)
        except Exception:
            # Any uncertain turn is latched by Mailbox. Never retry on a new engine.
            self.close()
            raise
        finally:
            self.active_turn = None
            self.lock.release()

    def _turn(self, message, request_id, presentation, on_delta):
        started = time.monotonic()
        self._ensure_server()
        state = self.session(presentation=presentation is not None)
        thread_id = state.get('thread_id')
        if not thread_id or thread_id not in self.loaded:
            params = {'cwd': self.workspace, 'approvalPolicy': 'never', 'sandbox': 'read-only'}
            instructions = character_instructions(presentation)
            if instructions:
                params['developerInstructions'] = instructions
            if thread_id:
                params['threadId'] = thread_id
            result = self._rpc('thread/resume' if thread_id else 'thread/start', params)
            thread_id = result['thread']['id']
            self.loaded.add(thread_id)
            state.update(thread_id=thread_id, updated_at=time.time(), transport='app-server')
            write_json(self.presentation_session_file if presentation is not None else self.session_file, state)
        if self.cancelled.is_set():
            raise TurnFailure('Turn was cancelled during startup')
        prompt = character_prompt(message, presentation) if presentation is not None else POLICY + message
        result = self._rpc('turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': prompt}]})
        turn_id = result['turn']['id']
        self.active_turn = (thread_id, turn_id)
        items, order = {}, []
        first_delta = None
        last_emit = 0
        token_usage = None

        def public_text():
            return '\n'.join(items[key]['text'] for key in order if items[key]['allowed'])[:8000]

        def emit(force=False):
            nonlocal last_emit, first_delta
            text = public_text()
            if text and (force or time.monotonic() - last_emit >= .1):
                if first_delta is None:
                    first_delta = time.monotonic() - started
                last_emit = time.monotonic()
                if on_delta:
                    on_delta(text, turn_id)

        while time.monotonic() - started < self.timeout:
            if self.cancelled.is_set():
                raise TurnFailure('Turn was cancelled; late answer was discarded')
            try:
                event = self.notifications.get(timeout=.1)
            except queue.Empty:
                emit()
                continue
            method = event.get('method')
            if method == '_closed':
                raise TurnFailure('Owned engine exited; request was not replayed')
            params = event.get('params') or {}
            if params.get('threadId') != thread_id:
                continue
            if method == 'thread/tokenUsage/updated':
                token_usage = (params.get('tokenUsage') or {}).get('last')
                continue
            if params.get('turnId', (params.get('turn') or {}).get('id')) != turn_id:
                continue
            if method in ('item/started', 'item/completed'):
                item = params.get('item') or {}
                key = item.get('id')
                allowed = item.get('type') == 'agentMessage' and item.get('phase') in PUBLIC_PHASES
                if key and allowed:
                    if key not in items:
                        order.append(key)
                        items[key] = {'allowed': True, 'text': ''}
                    if method == 'item/completed':
                        items[key]['text'] = str(item.get('text', ''))[:8000]
                    emit()
                elif key in items:
                    items[key]['allowed'] = False
            elif method == 'item/agentMessage/delta':
                key = params.get('itemId')
                # Unknown item/phase never streams to UI or speech.
                if key in items and items[key]['allowed']:
                    items[key]['text'] = (items[key]['text'] + str(params.get('delta', '')))[:8000]
                    emit()
            elif method == 'turn/completed':
                turn = params.get('turn') or {}
                if turn.get('status') != 'completed' or self.cancelled.is_set():
                    raise TurnFailure('Engine turn interrupted or failed; partial answer is incomplete')
                text = public_text()
                if not text.strip():
                    raise TurnFailure('Engine did not return an allowlisted completed answer')
                emit(True)
                return {'thread_id': thread_id, 'turn_id': turn_id, 'text': text, 'completed_at': time.time(),
                        'timing': {'first_public_delta_s': first_delta, 'completed_s': time.monotonic() - started},
                        'token_usage_last': token_usage}
        self.cancel()
        raise TurnFailure('Model turn timed out; no retry or action replay')
