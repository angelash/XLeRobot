import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from deployment.onboard.brain import Brain, TurnFailure, write_json
from deployment.onboard.service import Mailbox, handler_for, monitor_parent
from deployment.onboard.sync_source import sync_once


class FakeBrain:
    def __init__(self):
        self.calls = []
        self.cancelled = threading.Event()

    def session(self):
        return {"thread_id": "test-thread"}

    def ask(self, message, request_id):
        self.calls.append(request_id)
        return {"text": "received: " + message, "thread_id": "test-thread"}

    def cancel(self):
        self.cancelled.set()


def wait_done(mailbox, request_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = mailbox.read(request_id)
        if value["status"] not in ("queued", "running"):
            return value
        time.sleep(.01)
    pytest.fail("Worker did not finish")


def test_message_redelivery_is_idempotent_and_cannot_change_content(tmp_path):
    brain = FakeBrain()
    mailbox = Mailbox(brain, tmp_path)
    threading.Thread(target=mailbox.work, daemon=True).start()
    mailbox.submit("same-id", "hello")
    assert wait_done(mailbox, "same-id")["status"] == "completed"
    assert mailbox.submit("same-id", "hello")["status"] == "completed"
    assert brain.calls == ["same-id"]
    with pytest.raises(ValueError):
        mailbox.submit("same-id", "a different instruction")


def test_restart_interrupts_pending_work_instead_of_replaying_it(tmp_path):
    mailbox = Mailbox(FakeBrain(), tmp_path)
    mailbox.submit("pending", "do something later")
    restarted = Mailbox(FakeBrain(), tmp_path)
    assert restarted.read("pending")["status"] == "interrupted"
    assert restarted.queue.empty()
    with pytest.raises(TurnFailure):
        restarted.submit("next", "new request")
    restarted.reset()
    assert restarted.submit("next", "new request")["status"] == "queued"


def test_failure_latches_fault_and_cancels_queued_successors(tmp_path):
    class Failing(FakeBrain):
        def ask(self, *_args):
            raise TurnFailure("model network timeout")
    mailbox = Mailbox(Failing(), tmp_path)
    mailbox.submit("first", "first")
    mailbox.submit("second", "second")
    threading.Thread(target=mailbox.work, daemon=True).start()
    assert wait_done(mailbox, "first")["status"] == "failed"
    assert wait_done(mailbox, "second")["status"] == "cancelled"
    assert mailbox.health()["fault"]
    assert mailbox.health()["motion_enabled"] is False


def test_engine_timeout_terminates_owned_process_and_never_returns_success(tmp_path):
    engine = tmp_path / "slow.py"
    engine.write_text("import sys,time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")
    brain = Brain(sys.executable, tmp_path, tmp_path, tmp_path / "state", timeout=.2)
    brain.command = lambda _thread=None: [sys.executable, str(engine)]
    started = time.monotonic()
    with pytest.raises(TurnFailure, match="timed out"):
        brain.ask("hello", "timeout-test")
    assert time.monotonic() - started < 8
    assert brain.process is None
    assert brain.cancelled.is_set()


def test_partial_model_answer_is_not_a_completed_turn(tmp_path):
    engine = tmp_path / "partial.py"
    engine.write_text("import json\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'partial'}}))\n", encoding="utf-8")
    brain = Brain(sys.executable, tmp_path, tmp_path, tmp_path / "state")
    brain.command = lambda _thread=None: [sys.executable, str(engine)]
    with pytest.raises(TurnFailure, match="completed answer"):
        brain.ask("hello", "partial-test")


def test_http_roundtrip_and_cross_origin_rejection(tmp_path):
    mailbox = Mailbox(FakeBrain(), tmp_path)
    threading.Thread(target=mailbox.work, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(mailbox))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = "http://127.0.0.1:" + str(server.server_port)
    payload = json.dumps({"id": "http-test", "message": "hello"}).encode()
    try:
        request = urllib.request.Request(endpoint + "/v1/messages", data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 202
        assert wait_done(mailbox, "http-test")["reply"]["text"] == "received: hello"
        request.add_header("Origin", "https://unrelated.example")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=3)
        assert error.value.code == 403
        with pytest.raises(ValueError):
            mailbox.submit("../../outside", "bad id")
    finally:
        server.shutdown()
        server.server_close()


def test_clean_source_fast_forwards_and_laptop_edits_are_preserved(tmp_path):
    git = shutil.which("git")
    if not git:
        pytest.skip("Git unavailable")
    origin = tmp_path / "origin.git"
    author = tmp_path / "author"
    replica = tmp_path / "laptop"

    def run(cwd, *args):
        return subprocess.run([git, "-C", str(cwd), *args], check=True, capture_output=True)

    subprocess.run([git, "init", "--bare", "--initial-branch=main", str(origin)], check=True, capture_output=True)
    subprocess.run([git, "clone", str(origin), str(author)], check=True, capture_output=True)
    run(author, "config", "user.name", "Source sync test")
    run(author, "config", "user.email", "source-sync@example.invalid")
    (author / "code.txt").write_text("v1", encoding="utf-8")
    run(author, "add", ".")
    run(author, "commit", "-m", "v1")
    run(author, "push", "origin", "main")
    subprocess.run([git, "clone", str(origin), str(replica)], check=True, capture_output=True)
    (author / "code.txt").write_text("v2", encoding="utf-8")
    run(author, "commit", "-am", "v2")
    run(author, "push", "origin", "main")
    (replica / "code.txt").write_text("laptop edit", encoding="utf-8")
    assert sync_once(git, replica)["state"] == "blocked_local_changes"
    assert (replica / "code.txt").read_text() == "laptop edit"
    run(replica, "checkout", "--", "code.txt")
    assert sync_once(git, replica)["state"] == "updated"
    assert (replica / "code.txt").read_text() == "v2"
    assert sync_once(git, replica)["state"] == "current"


def test_cancel_before_launch_never_spawns_engine(tmp_path, monkeypatch):
    brain = Brain("must-not-run", tmp_path, tmp_path, tmp_path / "state")
    brain.cancel()
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("Cancelled turn started an engine"))
    with pytest.raises(TurnFailure, match="before it started"):
        brain.ask("hello", "cancelled")


def test_parent_exit_cancels_engine_and_closes_listener(tmp_path):
    brain = FakeBrain()
    mailbox = Mailbox(brain, tmp_path)
    stopped = threading.Event()
    class Server:
        def shutdown(self):
            stopped.set()
    monitor_parent(Server(), mailbox, 12345, probe=lambda _pid: False)
    assert stopped.is_set()
    assert brain.cancelled.is_set()
    assert "parent exited" in mailbox.health()["fault"]
