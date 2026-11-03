"""Read-only conversation export and independently verified local backup.

No robot modules, account credentials, or live application databases are copied.
Raw conversation files may contain private material: keep --output Git-ignored.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import zipfile

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized(path):
    return str(path).replace("\\\\?\\", "").replace("\\", "/").rstrip("/").casefold()


def within(path, root):
    return normalized(path) == normalized(root) or normalized(path).startswith(normalized(root) + "/")


def local_time(value):
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value, timezone.utc).astimezone(BEIJING).isoformat()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(BEIJING).isoformat()


def text_content(content):
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if isinstance(p, dict) and "text" in p)


def parent_id(meta):
    source = meta.get("source", {})
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except (ValueError, TypeError):
            source = {}
    return source.get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id") if isinstance(source, dict) else None


def discover(homes):
    """Read indexes in SQLite read-only mode; also find rollouts absent from indexes."""
    versions, index_rows, errors = [], [], []
    seen_paths = set()
    for label, home in homes:
        db_path = home / "state_5.sqlite"
        if db_path.exists():
            try:
                with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                    columns = {r[1] for r in db.execute("pragma table_info(threads)")}
                    names = [c for c in ("id", "cwd", "title", "name", "rollout_path", "source", "created_at", "updated_at", "project_id") if c in columns]
                    for row in db.execute("select " + ",".join(names) + " from threads"):
                        index_rows.append({"home": label, **dict(zip(names, row))})
            except sqlite3.Error as exc:
                errors.append({"source": str(db_path), "error": str(exc)})
        paths = set()
        for folder in (home / "sessions", home / "archived_sessions"):
            if folder.exists():
                paths.update(folder.rglob("*.jsonl"))
        for row in index_rows:
            path = Path(row.get("rollout_path", ""))
            if row["home"] == label and path.is_file():
                paths.add(path)
        for path in sorted(paths):
            if normalized(path) in seen_paths:
                continue
            seen_paths.add(normalized(path))
            try:
                with path.open("rb") as stream:
                    first = json.loads(stream.readline())
                if first.get("type") != "session_meta":
                    raise ValueError("first record is not session_meta")
                meta = first["payload"]
                versions.append({"home": label, "path": str(path), "meta": meta, "bytes_at_inventory": path.stat().st_size})
            except (OSError, ValueError, KeyError) as exc:
                errors.append({"source": str(path), "error": str(exc)})
    return versions, index_rows, errors


def complete_prefix(path, size=None):
    """Freeze existing bytes, dropping only an incomplete final JSONL record."""
    with Path(path).open("rb") as stream:
        data = stream.read(size) if size is not None else stream.read()
    end = data.rfind(b"\n") + 1
    if end == 0:
        raise ValueError(f"No complete records in {path}")
    return data[:end], len(data) - end


def extract(data, thread_id):
    """Prefer native completed items over their duplicate response_item records."""
    native, responses, statuses = [], [], []
    turn = None
    for number, line in enumerate(data.splitlines(), 1):
        obj = json.loads(line)
        payload = obj.get("payload", {})
        stamp = obj.get("timestamp")
        if obj.get("type") == "turn_context":
            turn = payload.get("turn_id", turn)
        if obj.get("type") == "event_msg":
            kind = payload.get("type")
            if kind == "task_started":
                turn = payload.get("turn_id", turn)
            if kind in {"task_complete", "turn_aborted"}:
                statuses.append({"turn_id": payload.get("turn_id", turn), "kind": kind, "timestamp": stamp, "error": payload.get("error"), "raw_line": number})
            if kind == "item_completed":
                item = payload.get("item", {})
                if item.get("type") in {"UserMessage", "AgentMessage"}:
                    native.append({"id": item.get("id"), "role": "user" if item["type"] == "UserMessage" else "assistant", "phase": item.get("phase"), "text": text_content(item.get("content")), "timestamp_utc": stamp, "turn_id": payload.get("turn_id", turn), "raw_line": number, "thread_id": thread_id, "attachments": [p for p in item.get("content", []) if isinstance(p, dict) and "text" not in p]})
        if obj.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") in {"user", "assistant"}:
            content = text_content(payload.get("content"))
            if payload.get("role") == "user" and (content.startswith("# AGENTS.md instructions") or content.startswith("<environment_context>") or content.startswith("<permissions instructions>")):
                continue
            responses.append({"id": payload.get("id"), "role": payload["role"], "phase": payload.get("phase"), "text": content, "timestamp_utc": stamp, "turn_id": turn, "raw_line": number, "thread_id": thread_id, "attachments": []})
    # Native completed items are authoritative when present. Add only response-only
    # messages (e.g. an interrupted turn), not prompt-injected user context.
    messages = native if native else responses
    if native:
        native_ids = {m["id"] for m in native if m["id"]}
        native_bodies = {(m["role"], m["text"]) for m in native}
        for message in responses:
            if message["id"] and message["id"] not in native_ids and (message["role"], message["text"]) not in native_bodies:
                # In native files, response-only user entries include injected
                # context. Only actual assistant messages can be supplemented.
                if message["role"] == "assistant":
                    messages.append(message)
    unique, seen = [], set()
    for message in sorted(messages, key=lambda m: m["raw_line"]):
        key = message["id"] or (message["turn_id"], message["role"], message["text"])
        if key in seen:
            continue
        seen.add(key)
        message["timestamp_local"] = local_time(message["timestamp_utc"])
        unique.append(message)
    return unique, statuses


def transcript(messages, title):
    parts = [f"# {title}", "", "时间：Asia/Shanghai (UTC+08:00)。原始记录及工具输出见 raw/；本阅读稿仅提取用户和助手消息。", ""]
    for m in messages:
        phase = f" / {m['phase']}" if m.get("phase") else ""
        parts.extend([f"## {m['timestamp_local']} · {m['role']}{phase}", "", f"来源：`{m['thread_id']}`，回合 `{m.get('turn_id')}`，原始行 {m['raw_line']}，消息 `{m['id']}`。", "", m["text"], ""])
        if m.get("attachments"):
            parts.extend([f"附件引用：{len(m['attachments'])} 项（原记录保留；外部文件单独盘点）。", ""])
    return "\n".join(parts)


def capture(args):
    output = Path(args.output).resolve()
    if (output / "capture.json").exists():
        raise ValueError("Snapshot already exists; use a new output directory")
    output.mkdir(parents=True, exist_ok=True)
    versions, rows, errors = discover([(f"home-{i + 1}", Path(h)) for i, h in enumerate(args.home)])
    indexes = {r["id"]: r for r in reversed(rows)}
    selected = {v["meta"]["id"] for v in versions if within(v["meta"].get("cwd", ""), args.project)} | set(args.related_thread)
    if args.native_inventory:
        native = json.loads(Path(args.native_inventory).read_text(encoding="utf-8"))
        for project in native["projects"]:
            if project["workspace"] == "XLeRobot":
                selected.update(t["threadId"] for t in project["threads"])
    changed = True
    while changed:
        before = set(selected)
        for version in versions:
            meta = version["meta"]
            if parent_id(meta) in selected:
                selected.add(meta["id"])
            if meta["id"] in selected and meta.get("history_base"):
                selected.add(meta["history_base"]["thread_id"])
        changed = before != selected
    available = {v["meta"]["id"] for v in versions}
    missing_selected = sorted(selected - available)
    if missing_selected:
        raise ValueError(f"Selected conversations unavailable: {missing_selected}")
    scope_rows = [r for r in rows if within(r.get("cwd", ""), args.project) or within(r.get("cwd", ""), args.related_workspace)]
    indexed_project_missing = sorted({r["id"] for r in scope_rows if within(r.get("cwd", ""), args.project)} - available)
    if indexed_project_missing:
        raise ValueError(f"Project index has missing conversations: {indexed_project_missing}")
    dump(output / "discovery.json", {"scope_index_rows": scope_rows, "discovery_errors": errors, "scanned_rollout_files": len(versions), "unavailable_scope_rows": [r for r in scope_rows if r["id"] not in available]})
    started = datetime.now(timezone.utc).isoformat()
    records, messages_by_id, raw_by_id = [], {}, {}
    for version in versions:
        meta = version["meta"]
        tid = meta["id"]
        if tid not in selected:
            continue
        source = Path(version["path"])
        data, dropped = complete_prefix(source, version["bytes_at_inventory"])
        digest = hashlib.sha256(data).hexdigest()
        relative = Path("raw") / tid / f"{version['home']}-{digest[:12]}.jsonl"
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if sha(destination) != digest:
            raise ValueError(f"Copy readback mismatch: {destination}")
        own, statuses = extract(data, tid)
        record = {"thread_id": tid, "title": (indexes.get(tid, {}).get("name") or indexes.get(tid, {}).get("title") or tid), "workspace": "XLeRobot" if within(meta.get("cwd", ""), args.project) else "open-share", "source": str(source), "raw_path": relative.as_posix(), "bytes": len(data), "sha256": digest, "dropped_incomplete_tail_bytes": dropped, "source_size_at_capture_end": source.stat().st_size, "history_base": meta.get("history_base"), "forked_from_id": meta.get("forked_from_id"), "parent_thread_id": parent_id(meta), "own_messages": len(own), "own_user_messages": sum(m["role"] == "user" for m in own), "own_turns": len({m["turn_id"] for m in own if m.get("turn_id")}), "record_count": len(data.splitlines()), "first_message_local": own[0]["timestamp_local"] if own else None, "last_message_local": own[-1]["timestamp_local"] if own else None, "task_statuses": statuses}
        records.append(record)
        # Retain every variant; use the longest for reading and resolve forks.
        if tid not in raw_by_id or len(data) > len(raw_by_id[tid]):
            raw_by_id[tid] = data
            messages_by_id[tid] = own
    for record in records:
        history = record.get("history_base")
        if history:
            inherited = raw_by_id[history["thread_id"]]
            end = history["end_byte_offset"]
            if end > len(inherited) or not inherited[:end].endswith(b"\n"):
                raise ValueError(f"Invalid fork byte offset: {record['thread_id']}")
            # Validate the original unchanged parent bytes, not a rewritten header.
            if len(inherited[:end].splitlines()) != history["end_ordinal_exclusive"]:
                raise ValueError(f"Invalid fork ordinal: {record['thread_id']}")
    canonical = {tid: max((r for r in records if r["thread_id"] == tid), key=lambda r: r["bytes"]) for tid in selected}

    def inherited_messages(tid, stack=()):
        if tid in stack:
            raise ValueError("Cyclic history_base")
        history = canonical[tid].get("history_base")
        messages = []
        if history:
            source_id = history["thread_id"]
            parent_messages = inherited_messages(source_id, (*stack, tid))
            messages.extend(m for m in parent_messages if m["thread_id"] != source_id or m["raw_line"] <= history["end_ordinal_exclusive"])
        messages.extend(messages_by_id[tid])
        seen, result = set(), []
        for message in messages:
            key = message["id"] or (message["thread_id"], message["raw_line"])
            if key not in seen:
                result.append(message)
                seen.add(key)
        return result

    all_messages = []
    attachment_references = []
    for tid in sorted(selected):
        record = canonical[tid]
        full = inherited_messages(tid)
        path = output / "transcripts" / (tid + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(transcript(full, str(record["title"]).splitlines()[0][:180]), encoding="utf-8")
        for message in messages_by_id[tid]:
            all_messages.append(message)
            for attachment in message.get("attachments", []):
                attachment_references.append({"thread_id": tid, "message_id": message["id"], "reference": attachment})
    # Each own message appears once across the chronology; inherited messages are
    # only expanded in individual transcripts.
    all_messages.sort(key=lambda m: (m["timestamp_utc"], m["thread_id"], m["raw_line"]))
    (output / "messages.jsonl").write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in all_messages), encoding="utf-8")
    by_day = defaultdict(list)
    by_turn = defaultdict(list)
    for message in all_messages:
        by_day[message["timestamp_local"][:10]].append(message)
        by_turn[(message["thread_id"], message["turn_id"])].append(message)
    summaries = []
    for (tid, turn_id), messages in by_turn.items():
        users = [m for m in messages if m["role"] == "user"]
        assistants = [m for m in messages if m["role"] == "assistant"]
        finals = [m for m in assistants if m.get("phase") in {"final_answer", "final"}]
        last = (finals or assistants or [None])[-1]
        summaries.append({"thread_id": tid, "turn_id": turn_id, "date": messages[0]["timestamp_local"][:10], "start": messages[0]["timestamp_local"], "user": "\n".join(m["text"] for m in users), "answer": last["text"] if last else None, "answer_is_explicit_final": bool(finals), "raw_line": last["raw_line"] if last else messages[0]["raw_line"]})
    dump(output / "turns.json", summaries)
    for day, messages in sorted(by_day.items()):
        readable = []
        for message in messages:
            if message["role"] == "user" or message.get("phase") in {"final", "final_answer"}:
                readable.append(message)
            elif any(s["thread_id"] == message["thread_id"] and s["raw_line"] == message["raw_line"] for s in summaries):
                readable.append(message)
        path = output / "daily-evidence" / (day + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(transcript(readable, day + " 对话原文摘录（非整理稿）"), encoding="utf-8")
    dump(output / "attachment-references.json", attachment_references)
    snapshot = {"schema_version": 1, "capture_started_utc": started, "capture_finished_utc": datetime.now(timezone.utc).isoformat(), "daily_timezone": "Asia/Shanghai (UTC+08:00)", "project": str(Path(args.project).resolve()), "related_workspace": args.related_workspace, "selected_thread_ids": sorted(selected), "unique_threads": len(selected), "workspace_counts": dict(Counter(r["workspace"] for r in canonical.values())), "raw_variants": len(records), "raw_bytes": sum(r["bytes"] for r in records), "unique_own_messages": len(all_messages), "unique_own_turns": len(by_turn), "user_messages": sum(m["role"] == "user" for m in all_messages), "daily_message_counts": {d: len(m) for d, m in sorted(by_day.items())}, "external_attachment_references": len(attachment_references), "records": records, "limitations": ["Snapshot consists of byte prefixes of live append-only files; subsequent turns are outside this cutoff.", "Raw files preserve inline attachments and tool records; external image/video files are references, not included unless explicitly collected.", "Old unavailable index entries are recorded in discovery.json; unavailable unrelated open-share history is not claimed complete."]}
    dump(output / "capture.json", snapshot)
    print(json.dumps({k: v for k, v in snapshot.items() if k not in {"records", "limitations"}}, ensure_ascii=False))


def attachments(args):
    root = Path(args.snapshot).resolve()
    references = json.loads((root / "attachment-references.json").read_text(encoding="utf-8"))
    search = defaultdict(list)
    for directory in args.search_root:
        for path in Path(directory).rglob("*"):
            if path.is_file():
                search[path.name].append(path)
    result = []
    inline = None
    for reference in references:
        original = Path(reference["reference"].get("path", ""))
        entry = {"thread_id": reference["thread_id"], "message_id": reference["message_id"], "original_path": str(original)}
        allowed = original.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
        if not allowed:
            entry["status"] = "unsupported_reference"
        else:
            candidates = [original] if original.is_file() else search.get(original.name, [])
            if len(candidates) != 1:
                entry["status"] = "missing" if not candidates else "ambiguous_relocation"
            else:
                source = candidates[0]
                if source.is_symlink() or source.stat().st_size > 128 * 1024 * 1024:
                    entry["status"] = "excluded_link_or_large_file"
                else:
                    data = source.read_bytes()
                    digest = hashlib.sha256(data).hexdigest()
                    target = root / "attachments" / (digest + original.suffix.lower())
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    if sha(target) != digest:
                        raise ValueError("Attachment readback mismatch")
                    entry.update(status="copied", resolved_path=str(source), archived_path=target.relative_to(root).as_posix(), bytes=len(data), sha256=digest)
        result.append(entry)
    # Temporary source images may be gone while the actual image supplied to
    # the conversation still exists inline. Preserve that distinct variant.
    missing = [entry for entry in result if entry["status"] == "missing"]
    if missing:
        wanted = {(entry["thread_id"], normalized(entry["original_path"])) for entry in missing}
        inline = {}
        capture = json.loads((root / "capture.json").read_text(encoding="utf-8"))
        for record in capture["records"]:
            if record["thread_id"] not in {entry["thread_id"] for entry in missing}:
                continue
            with (root / record["raw_path"]).open("rb") as stream:
                for number, line in enumerate(stream, 1):
                    obj = json.loads(line)
                    payload = obj.get("payload", {})
                    if obj.get("type") != "response_item" or payload.get("role") != "user":
                        continue
                    for path, data, mime in inline_images(payload.get("content", [])):
                        key = record["thread_id"], normalized(path)
                        if key in wanted:
                            inline[key] = data, mime, record["raw_path"], number
        for entry in missing:
            recovered = inline.get((entry["thread_id"], normalized(entry["original_path"])))
            if recovered:
                data, mime, raw_path, number = recovered
                digest = hashlib.sha256(data).hexdigest()
                extension = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp", "bmp": ".bmp"}[mime]
                target = root / "attachments" / (digest + extension)
                target.write_bytes(data)
                if sha(target) != digest:
                    raise ValueError("Inline attachment readback mismatch")
                entry.update(status="recovered_inline", original_status="missing", archived_path=target.relative_to(root).as_posix(), bytes=len(data), sha256=digest, raw_path=raw_path, raw_line=number, variant="conversation-encoded image; original bytes unverified")
    dump(root / "attachments-manifest.json", result)
    print(json.dumps({"references": len(result), "statuses": dict(Counter(e["status"] for e in result))}, ensure_ascii=False))


def inline_images(content):
    pending_path = None
    for part in content:
        if "text" in part:
            match = re.search(r'<image\b[^>]*\bpath="([^"]+)"', part["text"])
            if match:
                pending_path = match.group(1)
        if part.get("type") == "input_image" and pending_path:
            match = re.fullmatch(r"data:image/(jpeg|png|gif|webp|bmp);base64,([A-Za-z0-9+/=\r\n]+)", str(part.get("image_url", "")))
            if match:
                yield pending_path, base64.b64decode(match.group(2), validate=True), match.group(1)
            pending_path = None


def manifest(root):
    root = Path(root)
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Refuse symlink in snapshot: {path}")
        if path.is_file() and path != root / "manifest.json":
            files.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": sha(path)})
    value = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "files": files}
    dump(root / "manifest.json", value)
    return value


def verify_zip(path, expected_sha=None):
    path = Path(path)
    if expected_sha and sha(path) != expected_sha:
        raise ValueError("ZIP SHA-256 mismatch")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate ZIP paths")
        metadata = json.loads(archive.read("manifest.json"))
        expected = {f["path"] for f in metadata["files"]} | {"manifest.json"}
        if set(names) != expected:
            raise ValueError("ZIP inventory mismatch")
        for entry in metadata["files"]:
            data = archive.read(entry["path"])
            if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError(f"ZIP readback mismatch: {entry['path']}")
    return {"file": str(path.resolve()), "bytes": path.stat().st_size, "sha256": sha(path), "payload_files_verified": len(metadata["files"]), "verified_at": datetime.now(timezone.utc).isoformat()}


def backup(args):
    root = Path(args.snapshot).resolve()
    destination = Path(args.destination).resolve()
    if not root.is_dir():
        raise ValueError("Snapshot directory does not exist")
    if destination.exists():
        raise ValueError("Backup destination already exists")
    if root == destination or root in destination.parents:
        raise ValueError("Backup cannot be inside snapshot")
    metadata = manifest(root)
    if not metadata["files"]:
        raise ValueError("Snapshot has no payload files")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())
    result = verify_zip(destination)
    dump(destination.with_suffix(".receipt.json"), result)
    destination.with_suffix(".zip.sha256").write_text(result["sha256"] + "  " + destination.name + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("capture")
    command.add_argument("--project", required=True)
    command.add_argument("--related-workspace", required=True)
    command.add_argument("--home", action="append", required=True)
    command.add_argument("--related-thread", action="append", default=[])
    command.add_argument("--native-inventory")
    command.add_argument("--output", required=True)
    command = commands.add_parser("attachments")
    command.add_argument("--snapshot", required=True)
    command.add_argument("--search-root", action="append", default=[])
    command = commands.add_parser("backup")
    command.add_argument("--snapshot", required=True)
    command.add_argument("--destination", required=True)
    command = commands.add_parser("verify")
    command.add_argument("zip")
    command.add_argument("--sha256")
    args = parser.parse_args()
    if args.command == "capture":
        capture(args)
    elif args.command == "attachments":
        attachments(args)
    elif args.command == "backup":
        backup(args)
    else:
        print(json.dumps(verify_zip(args.zip, args.sha256), ensure_ascii=False))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
