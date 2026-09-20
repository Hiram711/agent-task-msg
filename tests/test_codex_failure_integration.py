"""Opt-in local integration: real Codex + loopback HTTP 500 + fake WeChat sender.

Set CODEX_TEST_BIN to an absolute codex.exe path. No external model/key required.
All Codex state lives under the test's temporary CODEX_HOME.
"""
import http.server
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest

from test_codex import IsolatedTest, ROOT, installer
sys.path.insert(0, str(ROOT / "scripts"))
from codex_rpc import CodexReader
from codex_watch import paths, run


@unittest.skipUnless(os.name == "nt" and os.environ.get("CODEX_TEST_BIN"),
                     "Set CODEX_TEST_BIN for the isolated real-Codex integration test")
class RealFailureTest(IsolatedTest):
    def test_real_http_failure_reaches_notification_pipeline(self):
        # Keep the native runtime in its own fixture process so Python/runtime
        # state cannot leak into other tests. The parent checks directory cleanup.
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--fixture", str(self.scratch)],
            capture_output=True, text=True, encoding="utf-8", timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-8000:])

    def _exercise_failure(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"PRIVATE simulated failure","type":"server_error"}}')
            def log_message(self, *args): pass
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        home = self.scratch / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text(f'''model = "notify-fixture"
model_provider = "notify-fixture"
[model_providers.notify-fixture]
name = "Local failure fixture"
base_url = "http://127.0.0.1:{httpd.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
''', encoding="utf-8")
        runtime = self.scratch / "notifier"
        (runtime / "scripts").mkdir(parents=True)
        (runtime / "scripts/wx_notify.ps1").write_bytes(installer.payload(ROOT / "scripts/wx_notify.ps1"))
        sender = runtime / "sender.ps1"
        sender.write_text('''param([string]$Target,[string]$MessageFile)
$body=[IO.File]::ReadAllText($MessageFile,[Text.Encoding]::UTF8)
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'captured.txt'),$body,[Text.Encoding]::UTF8)
exit 0
''', encoding="utf-8-sig")
        cfg = installer.load_json(ROOT / "config.example.json")
        cfg.update(enabled=True, presence_idle_min_seconds=0, sender_script=str(sender))
        self.write_json(runtime / "config.json", cfg)
        env = {**os.environ, "CODEX_HOME": str(home)}
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_THREAD_ID"):
            env.pop(key, None)
        exe = os.environ["CODEX_TEST_BIN"]
        process = subprocess.Popen([exe, "app-server", "--stdio"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding="utf-8", env=env,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        messages = queue.Queue()
        def read():
            for line in process.stdout:
                try: messages.put(json.loads(line))
                except ValueError: pass
        read_thread = threading.Thread(target=read, daemon=True)
        read_thread.start()
        def call(index, method, params):
            process.stdin.write(json.dumps({"id": index, "method": method, "params": params}) + "\n")
            process.stdin.flush()
            while True:
                value = messages.get(timeout=20)
                if value.get("id") == index:
                    self.assertNotIn("error", value)
                    return value["result"]
        worker = None
        errors = []
        thread_id = None
        try:
            call(1, "initialize", {"clientInfo": {"name": "failure_test", "version": "1"},
                                   "capabilities": {"experimentalApi": True}})
            process.stdin.write('{"method":"initialized"}\n')
            process.stdin.flush()
            thread_id = call(2, "thread/start", {"cwd": str(self.scratch), "model": "notify-fixture",
                                                "modelProvider": "notify-fixture", "approvalPolicy": "never",
                                                "sandbox": "read-only"})["thread"]["id"]
            # Establish persisted history. A thread with no submitted turns need not
            # yet be readable by a second App Server. This old failure is baseline.
            call(3, "turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "Baseline."}]})
            while True:
                event = messages.get(timeout=20)
                if event.get("method") == "turn/completed":
                    self.assertEqual(event["params"]["turn"]["status"], "failed")
                    break
            args = SimpleNamespace(thread_id=thread_id, codex_bin=exe, interval=0.25, max_seconds=40)
            def watch():
                try:
                    run(args, root=runtime, reader_factory=lambda path: CodexReader(path, env=env))
                except Exception as exc: errors.append(exc)
            worker = threading.Thread(target=watch, daemon=True)
            worker.start()
            status_path = paths(thread_id, runtime)["status"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if status_path.exists() and installer.load_json(status_path).get("state") == "running": break
                time.sleep(0.05)
            else: self.fail("Observer did not establish its baseline")
            self.assertFalse((runtime / "captured.txt").exists(), "Historical failure must not be sent")
            call(4, "turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "Reply only OK."}]})
            deadline = time.monotonic() + 20
            capture = runtime / "captured.txt"
            while time.monotonic() < deadline and not capture.exists(): time.sleep(0.1)
            self.assertTrue(capture.exists(), "Terminal HTTP 500 was not notified")
            body = capture.read_text(encoding="utf-8-sig")
            self.assertIn("[Codex] 出错中断", body)
            self.assertIn("失败", body)
            self.assertNotIn("PRIVATE", body)
            self.assertEqual(errors, [])
        finally:
            if worker and thread_id:
                paths(thread_id, runtime)["stop"].touch()
                worker.join(timeout=25)
            process.stdin.close()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.terminate(); process.wait(timeout=5)
            read_thread.join(timeout=1)
            if not read_thread.is_alive(): process.stdout.close()
            httpd.shutdown()
            httpd.server_close()
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fixture":
        fixture = RealFailureTest()
        fixture.scratch = Path(sys.argv[2])
        fixture._exercise_failure()
    else:
        unittest.main()
