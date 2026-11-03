"""Integrity checks using generated data; never touches app stores or hardware."""
import hashlib
import base64
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from argparse import Namespace

from archive_conversations import backup, complete_prefix, extract, inline_images, local_time, manifest, normalized, verify_zip


class ArchiveIntegrityTests(unittest.TestCase):
    def test_backup_rejects_missing_snapshot_without_creating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "missing"
            target = Path(directory) / "must-not-exist.zip"
            with self.assertRaisesRegex(ValueError, "does not exist"):
                backup(Namespace(snapshot=str(root), destination=str(target)))
            self.assertFalse(root.exists())
            self.assertFalse(target.exists())

    def test_backup_rejects_empty_snapshot_without_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "empty"
            root.mkdir()
            target = Path(directory) / "must-not-exist.zip"
            with self.assertRaisesRegex(ValueError, "no payload"):
                backup(Namespace(snapshot=str(root), destination=str(target)))
            self.assertFalse(target.exists())

    def test_inline_image_recovers_encoded_variant_with_exact_path(self):
        data = b"generated image test bytes"
        content = [{"type": "input_text", "text": '<image name="one" path="D:\\images\\missing.jpg">'}, {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(data).decode()}]
        self.assertEqual(list(inline_images(content)), [(r"D:\images\missing.jpg", data, "jpeg")])

    def test_live_tail_is_excluded_without_rewriting_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.jsonl"
            prefix = b'{"type":"session_meta"}\n'
            path.write_bytes(prefix + b'{"type":"unfinished')
            data, dropped = complete_prefix(path)
            self.assertEqual(data, prefix)
            self.assertGreater(dropped, 0)

    def test_nested_manifest_is_preserved_as_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence").mkdir()
            (root / "evidence/manifest.json").write_text('{"generated":true}', encoding="utf-8")
            value = manifest(root)
            self.assertEqual([item["path"] for item in value["files"]], ["evidence/manifest.json"])

    def test_native_and_response_duplicates_count_once(self):
        rows = [
            {"type": "event_msg", "timestamp": "2026-11-02T16:01:00Z", "payload": {"type": "task_started", "turn_id": "t1"}},
            {"type": "event_msg", "timestamp": "2026-11-02T16:01:01Z", "payload": {"type": "item_completed", "turn_id": "t1", "item": {"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": "test"}]}}},
            {"type": "response_item", "timestamp": "2026-11-02T16:01:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "test"}]}},
            {"type": "event_msg", "timestamp": "2026-11-02T16:01:02Z", "payload": {"type": "item_completed", "turn_id": "t1", "item": {"type": "AgentMessage", "id": "a1", "phase": "final_answer", "content": [{"type": "Text", "text": "done"}]}}},
            {"type": "response_item", "timestamp": "2026-11-02T16:01:02Z", "payload": {"type": "message", "role": "assistant", "id": "a1", "phase": "final_answer", "content": [{"type": "output_text", "text": "done"}]}},
        ]
        data = b"".join(json.dumps(r).encode() + b"\n" for r in rows)
        messages, _ = extract(data, "thread")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["timestamp_local"][:10], "2026-11-03")

    def test_snapshot_prefix_does_not_include_later_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.jsonl"
            first = b'{"first":true}\n'
            path.write_bytes(first + b'{"later":true}\n')
            data, _ = complete_prefix(path, len(first))
            self.assertEqual(data, first)

    def test_long_windows_prefix_normalizes(self):
        self.assertEqual(normalized(r"\\?\F:\workspace\XLeRobot"), normalized("f:/workspace/XLeRobot/"))

    def test_midnight_uses_project_timezone(self):
        self.assertEqual(local_time("2026-11-02T16:00:00Z"), "2026-11-03T00:00:00+08:00")

    def test_backup_verifier_detects_payload_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "snapshot"
            root.mkdir()
            (root / "generated.txt").write_text("original", encoding="utf-8")
            manifest(root)
            archive = Path(directory) / "backup.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.write(root / "manifest.json", "manifest.json")
                stream.writestr("generated.txt", "corrupted")
            with self.assertRaisesRegex(ValueError, "readback mismatch"):
                verify_zip(archive)

    def test_backup_verifier_rejects_extra_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "snapshot"
            root.mkdir()
            (root / "generated.txt").write_text("original", encoding="utf-8")
            manifest(root)
            archive = Path(directory) / "backup.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.write(root / "manifest.json", "manifest.json")
                stream.write(root / "generated.txt", "generated.txt")
                stream.writestr("unexpected.txt", "unexpected")
            with self.assertRaisesRegex(ValueError, "inventory mismatch"):
                verify_zip(archive)

    def test_valid_backup_is_read_back_and_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "snapshot"
            root.mkdir()
            (root / "generated.txt").write_text("generated test data", encoding="utf-8")
            manifest(root)
            archive = Path(directory) / "backup.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                for path in root.iterdir():
                    stream.write(path, path.name)
            report = verify_zip(archive)
            self.assertEqual(report["payload_files_verified"], 1)
            self.assertEqual(report["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
