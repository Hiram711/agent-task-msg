"""Read-only access to persisted Codex turns through the official App Server API."""
import json
import os
import queue
import subprocess
import threading
import time


class RpcError(RuntimeError):
    pass


class CodexReader:
    def __init__(self, executable, *, env=None, timeout=20, extra_args=()):
        self.timeout = timeout
        self.serial = 0
        self.responses = queue.Queue()
        self.proc = subprocess.Popen(
            [str(executable), "app-server", "--stdio", *extra_args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.reader_thread = threading.Thread(target=self._read, daemon=True)
        self.reader_thread.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "agent_task_msg_reader", "version": "1"},
                                     "capabilities": {"experimentalApi": True}})
            self._write({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                # This client never starts/resumes turns or answers server requests.
                # Ignore notifications to keep the queue bounded to our RPC responses.
                if "id" in value and ("result" in value or "error" in value):
                    self.responses.put(value)
        finally:
            self.responses.put(None)
            self.proc.stdout.close()

    def _write(self, value):
        try:
            self.proc.stdin.write(json.dumps(value) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RpcError("App Server connection closed") from exc

    def call(self, method, params):
        if method not in ("initialize", "thread/read", "thread/turns/list"):
            raise RpcError("Read-only client: method not allowed")
        self.serial += 1
        request_id = self.serial
        self._write({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                value = self.responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise RpcError("App Server read timed out") from exc
            if value is None:
                raise RpcError("App Server exited")
            if value.get("id") != request_id:
                continue
            if "error" in value:
                # No arbitrary server error text in log/notification: may contain input.
                code = value["error"].get("code", "unknown")
                raise RpcError(f"{method} failed (RPC code {code})")
            return value.get("result", {})

    def thread(self, thread_id):
        result = self.call("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise RpcError("Invalid thread/read response")
        return thread

    def turns(self, thread_id, known_ids=()):
        """Read through new pages to a known turn. Never load conversation items."""
        turns, cursor, visited = [], None, set()
        known = set(known_ids)
        for _ in range(100):
            params = {"threadId": thread_id, "limit": 100, "sortDirection": "desc", "itemsView": "notLoaded"}
            if cursor:
                params["cursor"] = cursor
            page = self.call("thread/turns/list", params)
            data = page.get("data")
            if not isinstance(data, list) or any(not isinstance(t, dict) or not isinstance(t.get("id"), str) for t in data):
                raise RpcError("Invalid thread/turns/list response")
            turns.extend(data)
            cursor = page.get("nextCursor")
            if not cursor or any(t["id"] in known for t in data):
                return turns
            if cursor in visited:
                raise RpcError("Repeated turn pagination cursor")
            visited.add(cursor)
        raise RpcError("Turn pagination limit exceeded; no checkpoint advanced")

    def close(self):
        # EOF lets App Server close its stores/helpers before falling back to kill.
        self.proc.stdin.close()
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        self.reader_thread.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
