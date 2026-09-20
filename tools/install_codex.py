#!/usr/bin/env python3
"""Install the Windows notification skill and merge its Codex PermissionRequest hook.

Dry runs never write. Installation preserves config.json/state and other hooks.
Uninstall removes only this integration's hook, retaining the skill and its data.
"""
import argparse
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
OWNER = "agent-task-msg-codex"
OWNED_COMMAND = re.compile(r"(?:^|\s)-HookOwner\s+agent-task-msg-codex(?:\s|$)", re.I)


def load_json(path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def merge_hooks(data, command=None):
    """Remove only owned handlers (even in mixed groups), then optionally install."""
    result = copy.deepcopy(data)
    hooks = result.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be a JSON object")
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{event} must be an array")
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"Invalid hook group in {event}")
            if not any(owned(handler) for handler in group["hooks"]):
                kept.append(group)
                continue
            group["hooks"] = [handler for handler in group["hooks"] if not owned(handler)]
            if group["hooks"]:
                kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if command:
        hooks.setdefault("PermissionRequest", []).append({
            "hooks": [{"type": "command", "command": command, "timeout": 15,
                       "statusMessage": "WeChat permission notification"}]
        })
    if hooks:
        result["hooks"] = hooks
    else:
        result.pop("hooks", None)
    return result


def hook_command(destination):
    # EncodedCommand avoids cmd/PowerShell quoting differences for spaces, &, $, %,
    # parentheses and Chinese paths. Only a fixed script path is encoded, never input.
    import base64
    path = str(destination / "hooks" / "dispatch.ps1").replace("'", "''")
    script = f"& '{path}' -Agent Codex -Kind needs_input -HookOwner {OWNER}"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + encoded


def owned(handler):
    """Identify only this installer's encoded invocation, including an older location."""
    import base64
    if not isinstance(handler, dict):
        return False
    for key in ("command", "commandWindows", "command_windows"):
        command = str(handler.get(key, ""))
        match = re.fullmatch(
            r"powershell\.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
            r"-EncodedCommand ([A-Za-z0-9+/=]+)", command, re.I)
        if match:
            try:
                script = base64.b64decode(match[1], validate=True).decode("utf-16-le")
            except (ValueError, UnicodeError):
                continue
            if OWNED_COMMAND.search(script):
                return True
    return False


def runtime_files():
    files = [ROOT / "SKILL.md", ROOT / "config.example.json"]
    for folder in ("hooks", "scripts", "references", "tools"):
        files.extend(path for path in (ROOT / folder).rglob("*")
                     if path.is_file() and path.suffix in (".ps1", ".py", ".md")
                     and "__pycache__" not in path.parts
                     and not path.name.startswith("_"))
    return files


def payload(path):
    if path.suffix == ".ps1":
        text = path.read_text(encoding="utf-8-sig").lstrip("\ufeff")
        return ("\ufeff" + text.replace("\r\n", "\n").replace("\r", "\n")
                .replace("\n", "\r\n")).encode("utf-8")
    return path.read_bytes()


def write_file(path, content, stamp):
    if path.exists() and path.read_bytes() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak-" + stamp)
        backup.write_bytes(path.read_bytes())
        print(f"Backup: {backup}")
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove only owned hooks; keep skill files, config and state")
    parser.add_argument("--sender-script", type=Path,
                        help="Explicit path to wechat-send/scripts/wx_send.ps1")
    args = parser.parse_args(argv)
    if os.name != "nt" and not args.dry_run and not args.uninstall:
        parser.error("The sender requires Windows; use --dry-run to preview elsewhere")
    try:
        home = args.codex_home.expanduser().resolve()
        destination = home / "skills" / "agent-task-msg"
        hooks_path = home / "hooks.json"
        original = load_json(hooks_path)
        updated = merge_hooks(original, None if args.uninstall else hook_command(destination))
        pending = []
        if not args.uninstall:
            # Validate and prepare everything before changing any files.
            cfg_path = destination / "config.json"
            config = load_json(cfg_path if cfg_path.exists() else ROOT / "config.example.json")
            if args.sender_script:
                sender = args.sender_script.expanduser().resolve()
                if not sender.is_file():
                    raise ValueError(f"Sender script does not exist: {sender}")
                config["sender_script"] = str(sender)
            for source in runtime_files():
                pending.append((destination / source.relative_to(ROOT), payload(source)))
            if not cfg_path.exists() or args.sender_script:
                pending.append((cfg_path, json_bytes(config)))
        if updated != original:
            pending.append((hooks_path, json_bytes(updated)))
        pending = [(p, content) for p, content in pending
                   if not p.exists() or p.read_bytes() != content]
        print(f"Codex home: {home}")
        print(f"Skill: {destination}")
        for path, _ in pending:
            print(f"{'Would write' if args.dry_run else 'Write'}: {path}")
        if args.dry_run:
            print(json.dumps(updated, ensure_ascii=False, indent=2))
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            for path, content in pending:
                write_file(path, content, stamp)
        if args.uninstall:
            print("Owned hooks removed (or previewed). Skill, config and state retained.")
        else:
            print("PermissionRequest hook installed; error watcher, question and manual-permission pre-notices are separate entry points.")
            print("After enabling notifications, start the current-thread error watcher:")
            print(f'  python "{destination / "scripts" / "codex_watch.py"}" start')
            print("Review/trust the hook in Codex (CLI: /hooks); never bypass hook trust.")
            print("New installs default to disabled; existing enabled/config values are preserved.")
            print("Enable only when requested: powershell -NoProfile -ExecutionPolicy Bypass "
                  f'-File "{destination / "scripts" / "wx_switch.ps1"}" -On')
        return 0
    except (OSError, ValueError) as error:
        print(f"Install failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
