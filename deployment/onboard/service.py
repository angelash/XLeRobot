"""Durable, loopback-only two-way mailbox for the laptop's Xiaojiu intelligence.

The scheduled boot task owns this process, so closing SSH or the workstation
does not terminate it. Conversation files and diagnostics stay outside Git.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import json
import os
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .brain import Brain, TurnFailure, write_json
from .persona import validate_presentation
from .sync_source import sync_once


IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class Mailbox:
    def __init__(self, brain, state_dir):
        self.brain = brain
        self.directory = Path(state_dir) / "messages"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.queue = queue.Queue(maxsize=16)
        self.guard = threading.RLock()
        self.active = None
        self.fault = None
        self.sync = {"state": "not_checked"}
        self.started_at = time.time()
        self.live_epoch = os.urandom(16).hex()
        self.live_seq = 0
        self.live_values = OrderedDict()
        self.changed = threading.Condition(self.guard)
        self.cancel_requests = set()
        for path in self.directory.glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value["status"] in ("queued", "running"):
                value.update(status="interrupted", error="Service restarted; request was not replayed")
                write_json(path, value)
                self.fault = "Previous service interrupted a request; inspect it before reset"

    def path(self, request_id):
        if not IDENTIFIER.fullmatch(request_id):
            raise ValueError("Invalid request id")
        return self.directory / (request_id + ".json")

    def read(self, request_id):
        return json.loads(self.path(request_id).read_text(encoding="utf-8"))

    def submit(self, request_id, message, sender="workstation", presentation=None):
        path = self.path(request_id)
        presentation = validate_presentation(presentation)
        if presentation is not None and sender != 'presence':
            raise ValueError('Presentation context requires the presence sender')
        if not isinstance(message, str) or not message.strip() or len(message) > 24000:
            raise ValueError("Message must contain 1-24000 characters")
        with self.guard:
            if path.exists():
                existing = self.read(request_id)
                if (existing["message"] != message or existing["sender"] != sender
                        or existing.get('presentation') != presentation):
                    raise ValueError("Request id already belongs to a different message")
                return existing
            if self.fault:
                raise TurnFailure("Bridge fault is latched; inspect it and reset before a new request")
            if self.queue.full():
                raise TurnFailure("Mailbox is full; retry later with the same request id")
            value = {"id": request_id, "sender": sender, "message": message, "status": "queued", "created_at": time.time()}
            if presentation is not None:
                value['presentation'] = presentation
            write_json(path, value)
            self.queue.put_nowait(request_id)
            self._publish(value)
            return value

    def _publish(self, value, text=None, turn_id=None):
        with self.changed:
            self.live_seq += 1
            public = {'id': value['id'], 'status': value['status'], 'message': value['message'][:4000],
                      'reply': text if text is not None else str(value.get('reply', {}).get('text', ''))[:8000],
                      'turn_id': turn_id or value.get('reply', {}).get('turn_id'),
                      'seq': self.live_seq, 'epoch': self.live_epoch}
            self.live_values[value['id']] = public
            self.live_values.move_to_end(value['id'])
            while len(self.live_values) > 32:
                self.live_values.popitem(last=False)
            self.changed.notify_all()

    def live(self, after, epoch):
        with self.guard:
            reset = epoch != self.live_epoch or after > self.live_seq
            return {'epoch': self.live_epoch, 'seq': self.live_seq, 'reset': reset,
                    'messages': [x for x in self.live_values.values() if reset or x['seq'] > after]}

    def health(self):
        with self.guard:
            return {"service": "xlerobot-onboard", "version": 1, "motion_enabled": False,
                    "presentation_context_version": 2,
                    "public_stream_version": 1, "engine_transport": 'app-server' if getattr(self.brain, 'supports_stream', False) else 'cli',
                    "active": self.active, "fault": self.fault, "queue": self.queue.qsize(),
                    "thread_id": self.brain.session().get("thread_id"), "source_sync": self.sync,
                    "started_at": self.started_at}

    def presentation(self):
        """Bounded public receipts; excludes raw engine events and diagnostics."""
        with self.guard:
            paths = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:32]
            messages = []
            for path in reversed(paths):
                value = json.loads(path.read_text(encoding="utf-8"))
                messages.append({"id": value["id"], "status": value["status"],
                                 "message": str(value.get("message", ""))[-4000:],
                                 "reply": str(value.get("reply", {}).get("text", ""))[:8000],
                                 "created_at": value.get("created_at"),
                                 "started_at": value.get("started_at"),
                                 "finished_at": value.get("finished_at")})
            return {"version": 1, "fault": bool(self.fault), "messages": messages}

    def reset(self):
        with self.guard:
            if self.active:
                raise TurnFailure("Cannot reset while a turn is running")
            self.fault = None
            self.brain.cancelled.clear()
            return self.health()

    def work(self):
        while True:
            request_id = self.queue.get()
            with self.guard:
                value = self.read(request_id)
                if value['status'] == 'cancelled':
                    self.queue.task_done()
                    continue
                if self.fault:
                    value.update(status="cancelled", error="A previous failure stopped the queue")
                    write_json(self.path(request_id), value)
                    self.queue.task_done()
                    continue
                self.active = request_id
                value.update(status="running", started_at=time.time())
                write_json(self.path(request_id), value)
                self._publish(value)
            try:
                kwargs = {'presentation': value['presentation']} if value.get('presentation') is not None else {}
                if getattr(self.brain, 'supports_stream', False):
                    def on_delta(text, turn_id):
                        with self.guard:
                            if request_id not in self.cancel_requests and not self.fault:
                                self._publish(value, text, turn_id)
                    kwargs['on_delta'] = on_delta
                result = self.brain.ask(value['message'], request_id, **kwargs)
                with self.guard:
                    if self.fault or request_id in self.cancel_requests:
                        raise TurnFailure("Turn was cancelled; late answer was discarded")
                    value.update(status="completed", reply=result)
            except Exception as exc:
                with self.guard:
                    if request_id in self.cancel_requests:
                        value.update(status='cancelled', error='Cancelled by user; late results discarded')
                    else:
                        self.fault = str(exc)
                        value.update(status="failed", error=str(exc))
            finally:
                with self.guard:
                    value["finished_at"] = time.time()
                    write_json(self.path(request_id), value)
                    self._publish(value)
                    if request_id in self.cancel_requests:
                        self.cancel_requests.discard(request_id)
                        self.brain.cancelled.clear()
                    self.active = None
                    self.queue.task_done()

    def cancel_request(self, request_id):
        with self.guard:
            value = self.read(request_id)
            if value.get('sender') != 'presence':
                raise ValueError('Only display conversations may be cancelled here')
            if value['status'] not in ('queued', 'running'):
                return {'id': request_id, 'status': value['status']}
            if request_id == self.active:
                self.cancel_requests.add(request_id)
                # Protocol replies are handled by the engine reader, which does
                # not take this mailbox lock. Serialize with worker completion.
                self.brain.cancel()
            else:
                value.update(status='cancelled', finished_at=time.time())
                write_json(self.path(request_id), value)
                self._publish(value)
        return {'id': request_id, 'status': 'cancelled'}

    def cancel(self, reason="Cancelled by operator; no request will replay automatically"):
        with self.guard:
            self.fault = reason
        self.brain.cancel()
        return self.health()


def handler_for(mailbox):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                if self.path == '/v1/presentation/stream':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.connection.settimeout(4)
                    cursor, epoch = 0, None
                    try:
                        while True:
                            batch = mailbox.live(cursor, epoch)
                            if batch['messages'] or batch['reset']:
                                self.wfile.write(('data: ' + json.dumps(batch, ensure_ascii=False) + '\n\n').encode('utf-8'))
                                self.wfile.flush()
                            else:
                                self.wfile.write(b': keepalive\n\n')
                                self.wfile.flush()
                            cursor, epoch = batch['seq'], batch['epoch']
                            with mailbox.changed:
                                if mailbox.live_seq == cursor:
                                    mailbox.changed.wait(timeout=2)
                    except (BrokenPipeError, ConnectionError, OSError):
                        return
                if self.path == "/health":
                    return self.reply(200, mailbox.health())
                if self.path == "/v1/presentation":
                    return self.reply(200, mailbox.presentation())
                if self.path.startswith("/v1/messages/"):
                    return self.reply(200, mailbox.read(self.path.removeprefix("/v1/messages/")))
                self.reply(404, {"error": "Unknown route"})
            except FileNotFoundError:
                self.reply(404, {"error": "Message does not exist"})
            except ValueError as exc:
                self.reply(400, {"error": str(exc)})

        def do_POST(self):
            try:
                expected_origin = "http://127.0.0.1:" + str(self.server.server_port)
                if self.headers.get("Origin") not in (None, expected_origin):
                    return self.reply(403, {"error": "Cross-origin requests are not accepted"})
                if self.headers.get_content_type() != "application/json":
                    return self.reply(415, {"error": "Use application/json"})
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 100000:
                    return self.reply(413, {"error": "Request body too large"})
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("JSON object required")
                if self.path == "/v1/messages":
                    return self.reply(202, mailbox.submit(payload["id"], payload["message"], payload.get("sender", "workstation"),
                                                         payload.get('presentation')))
                if self.path == "/v1/cancel":
                    return self.reply(200, mailbox.cancel())
                if self.path.startswith('/v1/messages/') and self.path.endswith('/cancel'):
                    return self.reply(200, mailbox.cancel_request(self.path[len('/v1/messages/'):-len('/cancel')]))
                if self.path == "/v1/reset":
                    return self.reply(200, mailbox.reset())
                self.reply(404, {"error": "Unknown route"})
            except (ValueError, KeyError, TypeError) as exc:
                self.reply(400, {"error": str(exc)})
            except TurnFailure as exc:
                self.reply(409, {"error": str(exc)})
            except FileNotFoundError:
                self.reply(404, {'error': 'Message does not exist'})

    return Handler


def parent_alive(pid):
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def monitor_parent(server, mailbox, pid, probe=parent_alive):
    while probe(pid):
        time.sleep(.2)
    mailbox.cancel("Service parent exited; owned engine and listener are stopping")
    if hasattr(mailbox.brain, 'close'):
        mailbox.brain.close()
    server.shutdown()


def sync_loop(mailbox, config):
    while True:
        try:
            value = sync_once(config["git"], config["workspace"])
        except Exception as exc:
            value = {"state": "unavailable", "error": str(exc), "at": time.time()}
        with mailbox.guard:
            mailbox.sync = value
        write_json(Path(config["state_dir"]) / "source-sync.json", value)
        time.sleep(max(15, int(config.get("sync_interval", 60))))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    # Explicit legacy selection is a maintenance fallback, never an uncertain
    # request retry. Default to the owned persistent protocol engine.
    if config.get('engine_transport', 'app-server') == 'app-server':
        from .app_server import AppServerBrain
        engine = AppServerBrain
    elif config.get('engine_transport') == 'cli':
        engine = Brain
    else:
        raise ValueError('Unknown engine transport')
    brain = engine(config["executable"], config["code_home"], config["workspace"],
                  Path(config["state_dir"]) / "brain", timeout=config.get("turn_timeout", 120))
    mailbox = Mailbox(brain, config["state_dir"])
    server = ThreadingHTTPServer(("127.0.0.1", config.get("port", 8781)), handler_for(mailbox))
    # Bind first: a second instance exits without starting a competing worker.
    threading.Thread(target=mailbox.work, daemon=True).start()
    if config.get("git"):
        threading.Thread(target=sync_loop, args=(mailbox, config), daemon=True).start()
    if args.parent_pid > 0:
        threading.Thread(target=monitor_parent, args=(server, mailbox, args.parent_pid), daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        mailbox.cancel()
        if hasattr(brain, 'close'):
            brain.close()
        server.server_close()


if __name__ == "__main__":
    main()
