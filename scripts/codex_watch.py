#!/usr/bin/env python3
"""Watch one Codex thread's persisted failed turns, independently of the model.

Uses official read-only App Server methods; never parses internal logs, resumes a
thread, answers approvals, or treats transport errors/interruptions as API failure.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from codex_notify import ROOT, notify
from codex_rpc import CodexReader, RpcError


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


class FailureTracker:
    def __init__(self, checkpoint=None):
        self.state = checkpoint or {"initialized": False, "seen": {}}
        if not isinstance(self.state.get("seen"), dict):
            raise ValueError("Invalid watcher checkpoint; do not replay history")

    def candidates(self, turns):
        if not self.state.get("initialized"):
            return []  # First snapshot is a baseline, not a backlog to notify.
        seen = self.state["seen"]
        return [t for t in reversed(turns)
                if t.get("status") == "failed" and seen.get(t["id"]) != "failed"]

    def record(self, turns):
        seen = self.state["seen"]
        for turn in reversed(turns):
            seen.pop(turn["id"], None)
            seen[turn["id"]] = turn.get("status", "unknown")
        self.state = {"initialized": True, "seen": dict(list(seen.items())[-4096:])}


def failure_summary(turn):
    # Raw provider errors may echo commands or secrets. Send only a typed code.
    error = turn.get("error") or {}
    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    code = info if isinstance(info, str) else next(iter(info), "unknown") if isinstance(info, dict) else "unknown"
    code = "".join(c for c in code if c.isalnum() or c in "_-")[:80]
    return f"Codex 回合已明确标记为失败。错误类型：{code or 'unknown'}。请回到 Codex 查看详情。"


@contextmanager
def single_instance(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def paths(thread_id, root=ROOT):
    key = hashlib.sha256(thread_id.encode()).hexdigest()[:24]
    folder = root / "state" / "codex-watch"
    return {name: folder / f"{key}.{suffix}" for name, suffix in
            (("checkpoint", "checkpoint.json"), ("status", "status.json"),
             ("lock", "lock"), ("stop", "stop"))}


def run(args, root=ROOT, reader_factory=CodexReader, emit=notify):
    files = paths(args.thread_id, root)
    reader = None
    with single_instance(files["lock"]):
        files["stop"].unlink(missing_ok=True)
        tracker = FailureTracker(load(files["checkpoint"]))
        deadline = time.monotonic() + args.max_seconds if args.max_seconds else float("inf")
        status = {"thread_id": args.thread_id, "pid": os.getpid(), "state": "starting"}
        def report(state, **values):
            status.update(state=state, updated_at=time.time(), **values)
            save(files["status"], status)
        report("starting")
        failures = 0
        try:
            while not files["stop"].exists() and time.monotonic() < deadline:
                try:
                    if reader is None:
                        reader = reader_factory(args.codex_bin)
                    thread = reader.thread(args.thread_id)
                    turns = reader.turns(args.thread_id, tracker.state["seen"])
                    events = tracker.candidates(turns)
                    for turn in events:
                        emit("error", failure_summary(turn), args.thread_id,
                             turn["id"], thread.get("cwd", ""), root=root)
                        # Save each handled turn before the next event to survive restart.
                        tracker.record([turn])
                        save(files["checkpoint"], tracker.state)
                    tracker.record(turns)
                    save(files["checkpoint"], tracker.state)
                    report("running", last_poll=time.time(), error=None,
                           handled_failures=status.get("handled_failures", 0) + len(events))
                    failures = 0
                except (OSError, ValueError, RpcError, RuntimeError) as exc:
                    failures += 1
                    # A reader failure is never a failure of the watched model turn.
                    report("degraded", error=type(exc).__name__, consecutive_read_failures=failures)
                    if reader:
                        reader.close()
                        reader = None
                wait_until = min(deadline, time.monotonic() + min(60, args.interval * max(1, failures)))
                while time.monotonic() < wait_until and not files["stop"].exists():
                    time.sleep(min(0.25, max(0, wait_until - time.monotonic())))
        finally:
            if reader:
                reader.close()
            report("stopped")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "run", "start", "stop", "status"))
    parser.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID"))
    parser.add_argument("--codex-bin", default=shutil.which("codex.exe") or shutil.which("codex"))
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--max-seconds", type=float, default=0,
                        help="Bounded test duration; 0 keeps watching until stop")
    args = parser.parse_args(argv)
    if not args.thread_id:
        parser.error("--thread-id or CODEX_THREAD_ID is required")
    if args.interval < 1 or args.max_seconds < 0:
        parser.error("interval must be >= 1 and max-seconds >= 0")
    files = paths(args.thread_id)
    try:
        if args.action == "status":
            status = load(files["status"])
            if status and status.get("state") != "stopped":
                status["stale"] = time.time() - status.get("updated_at", 0) > 120
            print(json.dumps(status or {"state": "not_started"}, ensure_ascii=False, indent=2))
        elif args.action == "stop":
            files["stop"].parent.mkdir(parents=True, exist_ok=True)
            files["stop"].touch()
            print("Stop requested; notification already in progress may finish first.")
        else:
            if not args.codex_bin:
                parser.error("Codex executable not found; pass --codex-bin")
            if args.action in ("check", "start"):
                with CodexReader(args.codex_bin) as reader:
                    thread = reader.thread(args.thread_id)
                    turns = reader.turns(args.thread_id)
                print(json.dumps({"thread_id": thread["id"], "stored_turns": len(turns),
                                  "reader": "official persisted turn API", "ok": True}))
            if args.action == "run":
                run(args)
            elif args.action == "start":
                command = [sys.executable, str(Path(__file__).resolve()), "run", "--thread-id", args.thread_id,
                           "--codex-bin", args.codex_bin, "--interval", str(args.interval),
                           "--max-seconds", str(args.max_seconds)]
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL,
                                           creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
                                           if os.name == "nt" else 0,
                                           start_new_session=os.name != "nt")
                print(f"Watcher launched (pid {process.pid}); use status to confirm it is running.")
        return 0
    except (OSError, ValueError, RpcError, RuntimeError) as exc:
        print(f"Watcher unavailable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
