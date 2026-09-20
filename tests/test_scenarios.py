import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

from test_codex import IsolatedTest, ROOT, PS, installer
sys.path.insert(0, str(ROOT / "scripts"))
from codex_notify import notify
from codex_rpc import CodexReader, RpcError
from codex_watch import FailureTracker, failure_summary, run, paths, single_instance


class FailurePolicyTests(unittest.TestCase):
    def test_baseline_and_terminal_failure_only(self):
        tracker = FailureTracker()
        baseline = [{"id": "old", "status": "failed"}, {"id": "active", "status": "inProgress"}]
        self.assertEqual(tracker.candidates(baseline), [])
        tracker.record(baseline)
        snapshot = [{"id": "complete", "status": "completed"},
                    {"id": "cancelled", "status": "interrupted"},
                    {"id": "retrying", "status": "inProgress", "error": {"message": "retry"}},
                    {"id": "active", "status": "failed"}, *baseline[:1]]
        self.assertEqual([t["id"] for t in tracker.candidates(snapshot)], ["active"])
        tracker.record(snapshot)
        restarted = FailureTracker(json.loads(json.dumps(tracker.state)))
        self.assertEqual(restarted.candidates(snapshot), [])
        self.assertEqual(len(restarted.candidates([{"id": "new", "status": "failed"}])), 1)

    def test_summary_never_forwards_raw_provider_error(self):
        summary = failure_summary({"error": {"message": "SECRET_TOKEN", "additionalDetails": "PRIVATE",
                                            "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 500}}}})
        self.assertIn("httpConnectionFailed", summary)
        self.assertNotIn("SECRET", summary)
        self.assertNotIn("PRIVATE", summary)

    def test_rpc_client_rejects_mutating_methods_without_transport(self):
        client = object.__new__(CodexReader)
        for method in ("thread/start", "thread/resume", "turn/start", "turn/interrupt"):
            with self.assertRaises(RpcError):
                client.call(method, {})

    def test_pagination_does_not_miss_failures_between_polls(self):
        client = object.__new__(CodexReader)
        pages = iter([{"data": [{"id": "new"}], "nextCursor": "p2"},
                      {"data": [{"id": "failed"}, {"id": "known"}], "nextCursor": "p3"}])
        client.call = lambda *args: next(pages)
        self.assertEqual([t["id"] for t in client.turns("thread", ["known"])], ["new", "failed", "known"])


class WatcherTests(IsolatedTest):
    def test_transport_failure_is_health_only_and_does_not_emit_model_error(self):
        calls = []
        class Broken:
            def __init__(self, *args): raise RpcError("offline")
        args = SimpleNamespace(thread_id="thread", codex_bin="unused", interval=0.01, max_seconds=0.05)
        run(args, root=self.scratch, reader_factory=Broken, emit=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])
        state = json.loads(paths("thread", self.scratch)["status"].read_text())
        self.assertEqual(state["state"], "stopped")
        self.assertGreater(state["consecutive_read_failures"], 0)
        self.assertFalse(paths("thread", self.scratch)["checkpoint"].exists())

    def test_singleton_prevents_duplicate_observers(self):
        with single_instance(self.scratch / "watch.lock"):
            with self.assertRaises(OSError):
                with single_instance(self.scratch / "watch.lock"):
                    self.fail("Second watcher acquired the same lock")

    def test_new_failure_is_handled_once_and_survives_restart(self):
        calls = []
        class Reader:
            polls = 0
            def __init__(self, *args): pass
            def thread(self, tid): return {"id": tid, "cwd": "project"}
            def turns(self, *args):
                Reader.polls += 1
                return [{"id": "turn", "status": "inProgress" if Reader.polls == 1 else "failed"}]
            def close(self): pass
        args = SimpleNamespace(thread_id="thread", codex_bin="unused", interval=0.01, max_seconds=0.07)
        run(args, root=self.scratch, reader_factory=Reader, emit=lambda *a, **k: calls.append(a))
        run(args, root=self.scratch, reader_factory=Reader, emit=lambda *a, **k: calls.append(a))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "error")
        self.assertEqual(calls[0][3], "turn")


@unittest.skipUnless(PS.is_file(), "Windows PowerShell required")
class QuestionTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        (self.scratch / "scripts").mkdir()
        (self.scratch / "scripts/wx_notify.ps1").write_bytes(installer.payload(ROOT / "scripts/wx_notify.ps1"))
        sender = self.scratch / "sender.ps1"
        sender.write_text('''param([string]$Target,[string]$MessageFile)
$body=[IO.File]::ReadAllText($MessageFile,[Text.Encoding]::UTF8)
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'delivered.txt'),$body,[Text.Encoding]::UTF8)
exit 0
''', encoding="utf-8-sig")
        self.cfg = installer.load_json(ROOT / "config.example.json")
        self.cfg.update(enabled=True, presence_idle_min_seconds=0, sender_script=str(sender))
        self.write_json(self.scratch / "config.json", self.cfg)

    def test_question_full_pipeline_and_trigger_gate(self):
        result = notify("question", "请选择中文方案 A 或 B。", "thread", "turn", "project", root=self.scratch)
        self.assertTrue(result.startswith("processed"))
        body = (self.scratch / "delivered.txt").read_text(encoding="utf-8-sig")
        self.assertIn("[Codex] 等待你回答", body)
        self.assertIn("请选择中文方案", body)
        self.assertFalse(list((self.scratch / "state").glob("codex_event_*.json")))
        self.cfg["triggers"]["question"] = False
        self.write_json(self.scratch / "config.json", self.cfg)
        self.assertEqual(notify("question", "another", "thread", root=self.scratch), "disabled")
        self.assertEqual((self.scratch / "delivered.txt").read_text(encoding="utf-8-sig"), body)

    def test_old_configuration_defaults_question_on_but_global_off_wins(self):
        del self.cfg["triggers"]["question"]
        self.cfg["enabled"] = False
        self.write_json(self.scratch / "config.json", self.cfg)
        self.assertEqual(notify("question", "hello", "thread", root=self.scratch), "disabled")
        self.assertFalse((self.scratch / "delivered.txt").exists())


if __name__ == "__main__":
    unittest.main()
