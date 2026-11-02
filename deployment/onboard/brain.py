"""Use the installed Xiaojiu engine and its existing profile, without copying auth.

The bridge is deliberately observation-only until navigation and the actuator
lease are accepted. A failed or ambiguous turn is never silently replayed.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from .persona import OBSERVATION_POLICY, character_instructions, character_prompt, validate_presentation


POLICY = '你是车载小九AI，与工作端小九协作。' + OBSERVATION_POLICY + '\n以下是工作端或笔记本用户发来的消息：\n'


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class TurnFailure(RuntimeError):
    pass


class Brain:
    def __init__(self, executable, code_home, workspace, state_dir, timeout=120):
        self.executable = str(executable)
        self.code_home = str(code_home)
        self.workspace = str(workspace)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.process = None
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.session_file = self.state_dir / "session.json"
        self.presentation_session_file = self.state_dir / 'presentation-session.json'

    def session(self, *, presentation=False):
        path = self.presentation_session_file if presentation else self.session_file
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {}

    def cancel(self):
        self.cancelled.set()
        process = self.process
        if process and process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                process.kill()

    def command(self, thread_id=None, *, presentation=None):
        command = [self.executable]
        instructions = character_instructions(presentation)
        if instructions is not None:
            # Initialize the display conversation with application instructions.
            # Resumed turns also carry their current role explicitly in the prompt.
            # Never change the account's config or replace the engine's base rules.
            command += ['-c', 'developer_instructions=' + json.dumps(instructions, ensure_ascii=False)]
        command += ["-a", "never", "-s", "read-only", "-C", self.workspace, "exec"]
        if thread_id:
            command += ["resume", "--json", "--skip-git-repo-check", thread_id, "-"]
        else:
            command += ["--json", "--skip-git-repo-check", "-"]
        return command

    def ask(self, message, request_id, *, presentation=None):
        presentation = validate_presentation(presentation)
        if not self.lock.acquire(blocking=False):
            raise TurnFailure("Another turn is running")
        try:
            return self._ask(message, request_id, presentation=presentation)
        finally:
            if self.process and self.process.poll() is None:
                self.cancel()
            self.process = None
            self.lock.release()

    def _ask(self, message, request_id, *, presentation=None):
        if self.cancelled.is_set():
            raise TurnFailure("Turn was cancelled before it started")
        is_presentation = presentation is not None
        state = self.session(presentation=is_presentation)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = self.code_home
        environment["PYTHONIOENCODING"] = "utf-8"
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        command = (self.command(state.get('thread_id'), presentation=presentation) if presentation is not None
                   else self.command(state.get('thread_id')))
        self.process = subprocess.Popen(command,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=environment,
                                        cwd=self.workspace, creationflags=creation_flags)
        if self.cancelled.is_set():
            self.cancel()
            self.process.communicate(timeout=10)
            raise TurnFailure("Turn was cancelled during startup")
        try:
            prompt = character_prompt(message, presentation) if presentation is not None else POLICY + message
            output, errors = self.process.communicate(prompt.encode("utf-8"), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.cancel()
            output, errors = self.process.communicate(timeout=10)
            self._save_output(request_id, output, errors)
            raise TurnFailure("Model turn timed out; no retry or action replay") from None
        self._save_output(request_id, output, errors)
        events = []
        for line in output.decode("utf-8", "replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        for event in events:
            if event.get("type") == "thread.started":
                state["thread_id"] = event["thread_id"]
                state["updated_at"] = time.time()
                write_json(self.presentation_session_file if is_presentation else self.session_file, state)
        messages = [event["item"]["text"] for event in events
                    if event.get("type") == "item.completed"
                    and event.get("item", {}).get("type") == "agent_message"]
        completed = any(event.get("type") == "turn.completed" for event in events)
        failed = any(event.get("type") in ("turn.failed", "error") for event in events)
        if self.process.returncode != 0 or not completed or failed or not messages:
            raise TurnFailure("Engine turn failed or did not return a completed answer; inspect private diagnostics")
        return {"thread_id": state.get("thread_id"), "text": "\n".join(messages), "completed_at": time.time()}

    def _save_output(self, request_id, output, errors):
        directory = self.state_dir / "diagnostics"
        directory.mkdir(exist_ok=True)
        (directory / (request_id + ".jsonl")).write_bytes(output)
        (directory / (request_id + ".stderr.log")).write_bytes(errors)
