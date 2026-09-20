#!/usr/bin/env python3
"""Explicit question or permission pre-notice; shares hook delivery settings."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent


def notify(kind, message, thread_id, turn_id="", cwd="", root=ROOT, *, approvals_reviewer="unknown"):
    """Wait for the sender/queue decision, never bypass enabled or presence gates.

    Exit 0 means the notification pipeline handled the event, not proof of delivery.
    Inspect state/notify.log for sent, skipped or queued. No message in argv.
    """
    if kind not in ("question", "permission_request", "error", "needs_input"):
        raise ValueError("Unsupported notification kind")
    cfg = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    triggers = cfg.get("triggers", {})
    default = triggers.get("needs_input", True) if kind == "permission_request" else True
    if not cfg.get("enabled", False) or not triggers.get(kind, default):
        return "disabled"
    # The caller supplies the CURRENT effective mode, not config.toml's default.
    # This filters routing before the request; it does not predict approval results.
    if kind == "permission_request" and approvals_reviewer != "user":
        return "skipped; permission pre-notice requires confirmed user review"
    if os.name != "nt":
        raise RuntimeError("WeChat delivery requires Windows")
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    payload = state / ("codex_event_" + uuid.uuid4().hex + ".json")
    data = {"agent": "Codex", "message": message, "session_id": thread_id,
            "turn_id": turn_id, "cwd": cwd,
            "request_key": hashlib.sha256((kind + "\0" + message).encode()).hexdigest()}
    if kind == "permission_request":
        data["approvals_reviewer"] = approvals_reviewer
    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    payload.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        # argv is constructed as a list; text is always passed in a UTF-8 file.
        result = subprocess.run([str(ps), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(root / "scripts/wx_notify.ps1"), "-Kind", kind,
                                 "-PayloadFile", str(payload)], capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(f"Notification worker failed (exit {result.returncode})")
        return "processed; check state/notify.log for delivery or skip reason"
    finally:
        payload.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("question", "permission_request"), default="question",
                        help="permission_request is a pre-notice, not proof of a pending approval")
    parser.add_argument("--approvals-reviewer", choices=("user", "auto_review", "guardian_subagent", "unknown"),
                        default="unknown", help="Current effective reviewer; only user allows a permission pre-notice")
    parser.add_argument("--message-file", type=Path, required=True,
                        help="UTF-8 question or permission-purpose summary; no credentials")
    parser.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID"))
    parser.add_argument("--turn-id", default="")
    parser.add_argument("--cwd", default=str(Path.cwd()))
    args = parser.parse_args(argv)
    if not args.thread_id:
        parser.error("--thread-id or CODEX_THREAD_ID is required")
    try:
        message = args.message_file.read_text(encoding="utf-8-sig").strip()
        if not message:
            parser.error("Notification summary is empty")
        print(notify(args.kind, message, args.thread_id, args.turn_id, args.cwd,
                     approvals_reviewer=args.approvals_reviewer))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
