"""Offline regression tests. All writes are isolated; sender never touches WeChat."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("install_codex", ROOT / "tools/install_codex.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
PS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.environ.get("AGENT_TASK_MSG_TEST_TMP", tempfile.gettempdir())).resolve()
        # mkdir's inherited ACL works in Windows restricted tokens, unlike mode 0700.
        self.scratch = self.base / ("agent-task-msg-test-" + uuid.uuid4().hex)
        self.scratch.mkdir(parents=True)

    def tearDown(self):
        target = self.scratch.resolve()
        assert target.parent == self.base and target.name.startswith("agent-task-msg-test-")
        shutil.rmtree(target)

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(installer.json_bytes(data))


class HookMergeTests(unittest.TestCase):
    def test_update_and_uninstall_preserve_mixed_handlers_and_metadata(self):
        old = installer.hook_command(Path("old location/skills/agent-task-msg"))
        other = {"type": "command", "command": "echo unrelated"}
        data = {"description": "user metadata", "hooks": {
            "PermissionRequest": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": old}, other]}],
            "Stop": [{"hooks": [other]}]}}
        new = installer.hook_command(Path("new location/skills/agent-task-msg"))
        merged = installer.merge_hooks(data, new)
        self.assertEqual(merged["description"], "user metadata")
        self.assertEqual(merged["hooks"]["PermissionRequest"][0],
                         {"matcher": "Bash", "hooks": [other]})
        self.assertEqual(merged["hooks"]["Stop"], data["hooks"]["Stop"])
        self.assertEqual(installer.merge_hooks(merged, new), merged)
        removed = installer.merge_hooks(merged)
        self.assertEqual(len(removed["hooks"]["PermissionRequest"]), 1)
        self.assertEqual(len(data["hooks"]["PermissionRequest"][0]["hooks"]), 2)

    def test_encoded_command_contains_literal_path_not_shell_expansion(self):
        path = Path("C:/中文 space & %TEMP% $()/it's/skills/agent-task-msg")
        command = installer.hook_command(path)
        decoded = base64.b64decode(command.split()[-1]).decode("utf-16-le")
        self.assertIn("it''s", decoded)
        self.assertIn("-Agent Codex", decoded)
        self.assertTrue(installer.owned({"command": command}))
        self.assertFalse(installer.owned({"command": "echo agent-task-msg-codex"}))

    def test_malformed_hook_structure_is_not_silently_overwritten(self):
        for value in ({"hooks": []}, {"hooks": {"Stop": {}}},
                      {"hooks": {"Stop": [{"hooks": "wrong"}]}}):
            with self.assertRaises(ValueError):
                installer.merge_hooks(value, "unused")


@unittest.skipUnless(os.name == "nt", "Windows installer integration")
class InstallTests(IsolatedTest):
    def run_installer(self, *args, expect=0):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/install_codex.py"),
             "--codex-home", str(self.scratch / "codex"), *args],
            capture_output=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=30)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def snapshot(self):
        return {p.relative_to(self.scratch): p.read_bytes()
                for p in self.scratch.rglob("*") if p.is_file()}

    def test_dry_run_does_not_create_home_or_modify_existing_files(self):
        self.run_installer("--dry-run")
        self.assertFalse((self.scratch / "codex").exists())
        self.run_installer()
        before = self.snapshot()
        self.run_installer("--dry-run")
        self.run_installer("--uninstall", "--dry-run")
        self.assertEqual(before, self.snapshot())

    def test_install_update_uninstall_preserves_user_configuration(self):
        home = self.scratch / "codex"
        hooks = home / "hooks.json"
        original = {"description": "keep", "hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "echo untouched"}]}]}}
        self.write_json(hooks, original)
        self.run_installer()
        skill = home / "skills/agent-task-msg"
        cfg = skill / "config.json"
        data = installer.load_json(cfg)
        self.assertIs(data["enabled"], False)
        self.assertTrue((skill / "hooks/dispatch.ps1").read_bytes().startswith(b"\xef\xbb\xbf"))
        data.update(enabled=True, target="我的会话", sender_script="custom.ps1")
        self.write_json(cfg, data)
        self.write_json(skill / "state/queue/example.json", {"body": "keep queue"})
        before = self.snapshot()
        self.run_installer()
        self.assertEqual(before, self.snapshot(), "repeat install must be a no-op")
        self.run_installer("--uninstall")
        self.assertEqual(installer.load_json(hooks), original)
        self.assertEqual(installer.load_json(cfg), data)
        self.assertTrue((skill / "state/queue/example.json").exists())
        self.assertTrue(list(home.glob("hooks.json.bak-*")))

    def test_invalid_json_and_missing_sender_leave_home_untouched(self):
        self.run_installer("--sender-script", str(self.scratch / "missing.ps1"), expect=1)
        self.assertFalse((self.scratch / "codex").exists())
        hooks = self.scratch / "codex/hooks.json"
        hooks.parent.mkdir()
        hooks.write_text("{broken", encoding="utf-8")
        before = self.snapshot()
        self.run_installer(expect=1)
        self.assertEqual(self.snapshot(), before)

    def test_explicit_sender_path_is_recorded_without_enabling(self):
        sender = self.scratch / "sender.ps1"
        sender.write_text("exit 0", encoding="utf-8")
        self.run_installer("--sender-script", str(sender))
        data = installer.load_json(self.scratch / "codex/skills/agent-task-msg/config.json")
        self.assertEqual(data["sender_script"], str(sender.resolve()))
        self.assertFalse(data["enabled"])


@unittest.skipUnless(PS.is_file(), "Windows PowerShell required")
class DispatchTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.runtime = self.scratch / "中文 space & %TEMP% $x it's" / "agent-task-msg"
        self.runtime.mkdir(parents=True)
        for relative in ("hooks/dispatch.ps1", "scripts/wx_notify.ps1"):
            target = self.runtime / relative
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(installer.payload(ROOT / relative))
        self.state = self.runtime / "state"
        self.state.mkdir()
        self.sender = self.runtime / "fake_sender.ps1"
        self.sender.write_text('''param([string]$Target, [string]$MessageFile)
$body = [IO.File]::ReadAllText($MessageFile, [Text.Encoding]::UTF8)
$result = @{target=$Target; body=$body} | ConvertTo-Json -Compress
$path = Join-Path $PSScriptRoot ('state/capture_' + [guid]::NewGuid().ToString('N') + '.json')
[IO.File]::WriteAllText($path, $result, (New-Object Text.UTF8Encoding($false)))
exit 0
''', encoding="utf-8-sig")
        self.config = installer.load_json(ROOT / "config.example.json")
        self.config.update(enabled=True, sender_script=str(self.sender),
                           presence_idle_min_seconds=0, dedupe_seconds=90)
        self.save_config()

    def save_config(self):
        self.write_json(self.runtime / "config.json", self.config)

    def event(self, **updates):
        data = {"hook_event_name": "PermissionRequest", "session_id": "session-one",
                "turn_id": "turn-one", "cwd": r"C:\项目\示例", "tool_name": "Bash",
                "tool_input": {"description": "需要访问网络", "command": "secret-command-token"}}
        data.update(updates)
        return data

    def captures(self):
        return [installer.load_json(p) for p in sorted(self.state.glob("capture_*.json"))]

    def dispatch(self, data, bom=False, codex=True):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        if bom:
            raw = b"\xef\xbb\xbf" + raw
        if codex:
            # Run the exact generated command through cmd, as well as its child PS 5.1.
            args = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c",
                    installer.hook_command(self.runtime)]
        else:
            args = [str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(self.runtime / "hooks/dispatch.ps1"), "-Kind", "needs_input"]
        start = time.monotonic()
        result = subprocess.run(args, input=raw, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"", "hook must not emit an approval decision")
        self.assertEqual(result.stderr, b"")
        self.assertLess(time.monotonic() - start, 15)
        deadline = time.monotonic() + 15
        while list(self.state.glob("hook_*.json")) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(list(self.state.glob("hook_*.json")), "background worker leaked payload")

    def test_utf8_bom_full_chain_and_claude_compatibility(self):
        self.dispatch(self.event(), bom=True)
        messages = self.captures()
        self.assertEqual(len(messages), 1)
        body = messages[0]["body"]
        for text in ("[Codex] 需要你处理", "项目：示例", "Bash", "需要访问网络"):
            self.assertIn(text, body)
        self.assertNotIn("secret-command-token", body)
        self.dispatch({"hook_event_name": "Notification", "message": "Claude 中文",
                       "cwd": r"C:\示例", "session_id": "claude"}, codex=False)
        self.assertTrue(any("[Claude Code]" in m["body"] for m in self.captures()))

    def test_deduplication_does_not_hide_different_sessions_turns_or_requests(self):
        self.dispatch(self.event())
        self.dispatch(self.event())
        self.assertEqual(len(self.captures()), 1)
        self.dispatch(self.event(session_id="session-two"))
        self.dispatch(self.event(turn_id="turn-two"))
        self.dispatch(self.event(tool_input={"description": "需要访问网络", "command": "other-command"}))
        self.assertEqual(len(self.captures()), 4)

    def test_disabled_and_disabled_trigger_do_not_send(self):
        self.config["enabled"] = False
        self.save_config()
        self.dispatch(self.event())
        self.config["enabled"] = True
        self.config["triggers"]["needs_input"] = False
        self.save_config()
        self.dispatch(self.event())
        self.assertEqual(self.captures(), [])

    def test_unknown_events_and_malformed_json_do_not_send(self):
        for data in (b"{invalid", b"null", b"[]", self.event(hook_event_name="Stop"),
                     self.event(hook_event_name="StopFailure")):
            self.dispatch(data)
        self.assertEqual(self.captures(), [])

    def test_diagnostics_distinguish_parse_failure_from_disabled_delivery(self):
        self.dispatch(b'{"secret-command-token": invalid')
        self.config['enabled'] = False
        self.save_config()
        self.dispatch(self.event())
        raw = (self.state / 'dispatch.jsonl').read_text(encoding='utf-8')
        entries = [json.loads(line) for line in raw.splitlines()]
        failures = [row for row in entries if row['stage'] == 'failed']
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['failed_stage'], 'parse_input')
        self.assertEqual(sum(row['stage'] == 'entered' for row in entries), 2)
        self.assertTrue(any(row['stage'] == 'worker_started' for row in entries))
        self.assertNotIn('secret-command-token', raw)
        self.assertNotIn('需要访问网络', raw)
        self.assertEqual(self.captures(), [])

    def test_unwritable_diagnostic_log_does_not_block_hook(self):
        (self.state / 'dispatch.jsonl').mkdir()
        self.dispatch(self.event())
        self.assertEqual(len(self.captures()), 1)

    def test_missing_description_uses_safe_fallback(self):
        self.dispatch(self.event(tool_input={"command": "secret-command-token"}))
        body = self.captures()[0]["body"]
        self.assertIn("Codex 正在等待权限批准", body)
        self.assertNotIn("secret-command-token", body)

    def test_null_tool_input_uses_fallback(self):
        self.dispatch(self.event(tool_input=None))
        self.assertEqual(len(self.captures()), 1)
        self.assertIn("Codex 正在等待权限批准", self.captures()[0]["body"])


if __name__ == "__main__":
    unittest.main()
