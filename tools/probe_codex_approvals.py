#!/usr/bin/env python3
"""Compare native Codex command and permission approvals with a local mock model.

Uses existing reviewed hooks and ephemeral threads; never grants test approvals.
Requires this skill's notifications to be OFF. Does not edit config or trust.
This tests the selected core, not the existing desktop task's live connection.
"""
import argparse
import gzip
import hashlib
import http.server
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time


class MockModel(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), ModelHandler)
        self.scenario = ""
        self.calls = 0
        self.failure = None


class ModelHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            body = json.loads(raw)
            server = self.server
            server.calls += 1
            if server.calls == 1:
                tools = []
                for spec in body.get("tools", []):
                    if spec.get("type") == "namespace":
                        tools.extend((t["name"], spec["name"]) for t in spec.get("tools", []))
                    elif spec.get("name"):
                        tools.append((spec["name"], None))
                match = next((t for t in tools if t[0] == server.scenario), None)
                if match is None:
                    raise RuntimeError("Required test tool unavailable: " + server.scenario)
                arguments = (
                    {"cmd": "Write-Output 'approval-probe'", "sandbox_permissions": "require_escalated",
                     "justification": "Harmless hook probe; the test client will deny this request."}
                    if server.scenario == "exec_command" else
                    {"permissions": {"network": {"enabled": True}}, "reason": "Local hook probe; grant nothing."})
                item = {"type": "function_call", "id": "fc_probe", "call_id": "call_probe",
                        "name": server.scenario, "arguments": json.dumps(arguments)}
                if match[1]:
                    item["namespace"] = match[1]
            else:
                item = {"type": "message", "id": "msg_probe", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "Probe finished."}]}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [
                {"type": "response.created", "response": {"id": "resp_probe"}},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {
                    "id": "resp_probe", "status": "completed", "output": [item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}]
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()
        except Exception as exc:
            # Only our fixed fixture error text/type, never request bodies or auth.
            self.server.failure = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            self.send_error(500, "Local fixture failed")


class ProbeClient:
    def __init__(self, executable, provider):
        env = dict(os.environ)
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_THREAD_ID"):
            env.pop(key, None)
        self.process = subprocess.Popen(
            [executable, "-c", provider, "-c", "features.request_permissions_tool=true",
             "-c", "features.plugins=false", "-c", "features.remote_plugin=false", "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        self.messages = queue.Queue()
        self.events = []
        self.serial = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.messages.put(None)
            self.process.stdout.close()

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def receive(self, timeout=25):
        value = self.messages.get(timeout=timeout)
        if value is None:
            raise RuntimeError("App Server exited")
        if "method" in value:
            self.events.append(value)
        return value

    def call(self, method, params):
        self.serial += 1
        request_id = self.serial
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            value = self.receive(timeout=max(0.1, deadline - time.monotonic()))
            if value.get("id") == request_id:
                if "error" in value:
                    raise RuntimeError(f"{method} failed (code {value['error'].get('code')})")
                return value["result"]
            if "id" in value and "method" in value:
                raise RuntimeError("Unexpected server request; stopping fixture")
        raise RuntimeError("App Server response timeout")

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.reader.join(timeout=2)


def diagnostic_stages(path, thread_id):
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue  # A concurrently appended line may not yet be complete.
    key = hashlib.sha256(thread_id.encode()).hexdigest()[:16].upper()
    traces = {r["trace"] for r in rows if r.get("session_key") == key}
    return [r["stage"] for r in rows if r.get("trace") in traces]


def exercise(client, server, scenario, cwd, runtime):
    server.scenario, server.calls, server.failure = scenario, 0, None
    response = client.call("thread/start", {
        "cwd": str(cwd), "ephemeral": True, "model": "notify-fixture", "modelProvider": "hook_probe",
        "approvalPolicy": "on-request", "approvalsReviewer": "user", "sandbox": "read-only"})
    if response.get("approvalPolicy") != "on-request" or response.get("approvalsReviewer") != "user":
        raise RuntimeError("Requested test approval policy was not applied")
    thread_id = response["thread"]["id"]
    start = len(client.events)
    client.call("turn/start", {"threadId": thread_id, "input": [{"type": "text",
        "text": "Local integration probe. Reject the test permission; do not execute commands."}]})
    approvals = []
    expected = "item/commandExecution/requestApproval" if scenario == "exec_command" else "item/permissions/requestApproval"
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        event = client.receive(timeout=max(0.1, deadline - time.monotonic()))
        method = event.get("method", "")
        if "id" in event and method:
            if method != expected:
                raise RuntimeError("Unexpected approval method; stopping fixture: " + method)
            approvals.append(method)
            print(f"{scenario}: approval pending; denying after 2 seconds", flush=True)
            time.sleep(2)
            result = {"permissions": {}} if scenario == "request_permissions" else {"decision": "decline"}
            client.send({"id": event["id"], "result": result})
        elif method == "turn/completed":
            if server.failure:
                raise RuntimeError(server.failure)
            if len(approvals) != 1 or event["params"]["turn"]["status"] != "completed":
                raise RuntimeError("Probe did not complete exactly one pending approval")
            break
    else:
        raise RuntimeError("Probe exceeded its deadline")
    observed = [v for v in client.events[start:] if v.get("params", {}).get("threadId") == thread_id]
    hooks = [{"method": v["method"], "event": v["params"]["run"].get("eventName"),
              "status": v["params"]["run"].get("status")}
             for v in observed if v["method"] in ("hook/started", "hook/completed")]
    commands = [v["params"]["item"]["status"] for v in observed if v["method"] == "item/completed"
                and v["params"].get("item", {}).get("type") == "commandExecution"]
    if scenario == "exec_command" and commands != ["declined"]:
        raise RuntimeError("Command was not reported as declined")
    return {"scenario": scenario, "approval_method": expected, "held_seconds": 2,
            "decision": "deny", "hook_events": hooks, "command_statuses": commands,
            "dispatch_stages": diagnostic_stages(runtime / "state/dispatch.jsonl", thread_id)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", default=shutil.which("codex.exe"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills/agent-task-msg")
    args = parser.parse_args()
    if os.name != "nt" or not args.codex_bin:
        parser.error("Windows and codex.exe are required")
    server = client = None
    try:
        cfg = json.loads((args.runtime / "config.json").read_text(encoding="utf-8-sig"))
        if cfg.get("enabled", False):
            raise RuntimeError("Turn notifications off before this probe; configuration is not changed automatically")
        args.output = args.output.resolve()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        server = MockModel()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        provider = ('model_providers.hook_probe={name="Local hook probe",'
                    f'base_url="http://127.0.0.1:{server.server_port}/v1",wire_api="responses",'
                    'requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}')
        client = ProbeClient(args.codex_bin, provider)
        client.call("initialize", {"clientInfo": {"name": "hook_approval_probe", "version": "1"},
                                   "capabilities": {"experimentalApi": True}})
        client.send({"method": "initialized"})
        discovered = client.call("hooks/list", {"cwds": [str(args.output.parent)]})["data"]
        if any(d["errors"] for d in discovered):
            raise RuntimeError("Hook discovery errors; inspect hooks/list first")
        enabled = [h for d in discovered for h in d["hooks"] if h["enabled"]]
        if (len(enabled) != 1 or enabled[0]["eventName"] != "permissionRequest"
                or enabled[0]["statusMessage"] != "WeChat permission notification"
                or enabled[0]["trustStatus"] != "trusted"):
            raise RuntimeError("Probe requires only the existing trusted WeChat permission hook; do not bypass trust")
        version = subprocess.check_output([args.codex_bin, "--version"], text=True, encoding="utf-8").strip()
        report = {"version": version, "ephemeral": True, "approval_reviewer": "user", "notifications_enabled": False,
                  "scope": "independent core process, not desktop live connection", "results": []}
        for scenario in ("exec_command", "request_permissions"):
            report["results"].append(exercise(client, server, scenario, args.output.parent, args.runtime))
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, queue.Empty, subprocess.SubprocessError) as exc:
        print("Approval probe incomplete: " + str(exc), file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()
        if server:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    sys.exit(main())
