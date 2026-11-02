"""Durable, loopback-only two-way mailbox for the laptop's Xiaojiu intelligence.

The scheduled boot task owns this process, so closing SSH or the workstation
does not terminate it. Conversation files and diagnostics stay outside Git.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .brain import Brain, TurnFailure, write_json
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

    def submit(self, request_id, message, sender="workstation"):
        path = self.path(request_id)
        if not isinstance(message, str) or not message.strip() or len(message) > 24000:
            raise ValueError("Message must contain 1-24000 characters")
        with self.guard:
            if path.exists():
                existing = self.read(request_id)
                if existing["message"] != message or existing["sender"] != sender:
                    raise ValueError("Request id already belongs to a different message")
                return existing
            if self.fault:
                raise TurnFailure("Bridge fault is latched; inspect it and reset before a new request")
            if self.queue.full():
                raise TurnFailure("Mailbox is full; retry later with the same request id")
            value = {"id": request_id, "sender": sender, "message": message, "status": "queued", "created_at": time.time()}
            write_json(path, value)
            self.queue.put_nowait(request_id)
            return value

    def health(self):
        with self.guard:
            return {"service": "xlerobot-onboard", "version": 1, "motion_enabled": False,
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
                if self.fault:
                    value.update(status="cancelled", error="A previous failure stopped the queue")
                    write_json(self.path(request_id), value)
                    self.queue.task_done()
                    continue
                self.active = request_id
                value.update(status="running", started_at=time.time())
                write_json(self.path(request_id), value)
            try:
                result = self.brain.ask(value["message"], request_id)
                with self.guard:
                    if self.fault:
                        raise TurnFailure("Turn was cancelled; late answer was discarded")
                    value.update(status="completed", reply=result)
            except Exception as exc:
                with self.guard:
                    self.fault = str(exc)
                    value.update(status="failed", error=str(exc))
            finally:
                with self.guard:
                    value["finished_at"] = time.time()
                    write_json(self.path(request_id), value)
                    self.active = None
                    self.queue.task_done()

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
                    return self.reply(202, mailbox.submit(payload["id"], payload["message"], payload.get("sender", "workstation")))
                if self.path == "/v1/cancel":
                    return self.reply(200, mailbox.cancel())
                if self.path == "/v1/reset":
                    return self.reply(200, mailbox.reset())
                self.reply(404, {"error": "Unknown route"})
            except (ValueError, KeyError, TypeError) as exc:
                self.reply(400, {"error": str(exc)})
            except TurnFailure as exc:
                self.reply(409, {"error": str(exc)})

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
    brain = Brain(config["executable"], config["code_home"], config["workspace"],
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
        server.server_close()


if __name__ == "__main__":
    main()
