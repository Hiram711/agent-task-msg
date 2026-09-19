#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI-Sender 安装器：把 hooks 注册进 ~/.claude/settings.json，并可选注册「解锁后补发」计划任务。

用法：
    python tools/install.py --dry-run      # 只打印将要做的改动
    python tools/install.py                # 安装（会先备份 settings.json）
    python tools/install.py --with-task    # 同时注册解锁补发计划任务
    python tools/install.py --uninstall    # 卸载（移除 hooks 与计划任务）

只增删本技能自己的 hook 条目（靠命令行里含 ai-sender-skill 路径识别），
不动用户其它 hook，可反复执行。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))
SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
DISPATCH = os.path.join(ROOT, "hooks", "dispatch.ps1")
NOTIFY = os.path.join(ROOT, "scripts", "wx_notify.ps1")
PS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                  "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
TASK_NAME = "AI-Sender-FlushOnUnlock"
MARK = "ai-sender-skill"   # 识别「我们自己的」hook 条目
SKILL_SRC = os.path.join(ROOT, "SKILL.md")
SKILL_NAME = "agent-task-msg"              # 要和 SKILL.md frontmatter 的 name 一致
SKILL_DST_DIR = os.path.join(os.path.expanduser("~"), ".claude",
                             "skills", SKILL_NAME)
SKILL_DST = os.path.join(SKILL_DST_DIR, "SKILL.md")

# 只要「Claude 被卡住了，自己发不了」这几种。
# 刻意不含 idle_prompt：那是「一轮干完在等下一句」，而干完这件事 Claude 自己会说，
# 两边都发就是连着两条、各夺前台 40 秒。
NOTIFY_MATCHER = ("permission_prompt|agent_needs_input"
                  "|elicitation_dialog|elicitation_url_dialog")


def check_sender():
    """装完提示发送技能（wechat-send）在不在。

    不在这儿重抄一份候选路径，而是问 wx_notify.ps1 -WhichSender —— 它才是运行时
    真正用的那份逻辑。抄一份的话两边会静默走样：安装时报"找到了"，真发时却找不到。

    找不到也照样装 hooks：用户可能打算先装这个、再去放发送技能。
    """
    try:
        p = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", NOTIFY, "-WhichSender"],
                           capture_output=True, timeout=60)
        out = p.stdout.decode("utf-8", "replace").strip()
        if p.returncode == 0 and out:
            return True, out
    except Exception as e:
        return False, "问 wx_notify.ps1 -WhichSender 失败：%s" % e
    return False, ("默认位置和 config.json 的 sender_script 都没有"
                   "（跑 scripts/wx_switch.ps1 -Status 看详情）")


def cmd(kind):
    c = '"%s" -NoProfile -ExecutionPolicy Bypass -File "%s"' % (PS, DISPATCH)
    if kind:
        c += " -Kind " + kind
    return c


def desired_hooks():
    """只注册 Claude 自己发不出通知的那两种情况。

    「任务干完了」和「我想问你一句」不在这儿：那两种 Claude 还活着、还能调工具，
    用户交代一句「离开一会儿，完事微信叫我」就够了，挂 hook 只会和它自己发的撞车。

    所以 Stop（长任务完成）和 UserPromptSubmit（只为给 Stop 算耗时）都不注册，
    对应的触发器、配置字段和回合计时代码也在 2026-09-19 一并删掉了 —— 半留半删
    的残留只会让人以为还有第三条路。真要加回来，是一整套一起加。
    """
    return {
        # 需要我确认/授权时 Claude 被挂起，调不了任何工具，只能靠 hook 替它喊
        "Notification": [
            {"matcher": NOTIFY_MATCHER,
             "hooks": [{"type": "command", "command": cmd("needs_input"), "timeout": 15}]}
        ],
        # 回合因 API 错误结束。每回合最多一次，重试成功不算，所以响了就是真停住了。
        # 不加 matcher = 所有错误类型都报
        "StopFailure": [
            {"hooks": [{"type": "command", "command": cmd("error"), "timeout": 15}]}
        ],
    }


def is_ours(group):
    """这个 matcher 组里有没有我们自己的 handler。"""
    for h in group.get("hooks", []):
        if MARK in str(h.get("command", "")):
            return True
    return False


def strip_ours(hooks_obj):
    """移除所有属于本技能的条目，保留用户其它 hook。返回移除条数。"""
    removed = 0
    for event in list(hooks_obj.keys()):
        groups = hooks_obj.get(event) or []
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            if not isinstance(g, dict):
                kept.append(g)
                continue
            inner = g.get("hooks", [])
            mine = [h for h in inner if MARK in str(h.get("command", ""))]
            if not mine:
                kept.append(g)
                continue
            removed += len(mine)
            rest = [h for h in inner if MARK not in str(h.get("command", ""))]
            if rest:                       # 同组里还有用户自己的 handler，保留它们
                g["hooks"] = rest
                kept.append(g)
        if kept:
            hooks_obj[event] = kept
        else:
            del hooks_obj[event]
    return removed


def load_settings():
    if not os.path.exists(SETTINGS):
        return {}
    with open(SETTINGS, "r", encoding="utf-8-sig") as f:
        txt = f.read().strip()
    return json.loads(txt) if txt else {}


def save_settings(data):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS)


def backup_settings():
    if not os.path.exists(SETTINGS):
        return None
    dst = "%s.bak-%s" % (SETTINGS, datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(SETTINGS, dst)
    return dst


def normalize_ps1():
    """所有 .ps1 统一成 CRLF + 单个 BOM，否则 PowerShell 5.1 会解析失败。"""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    from pathlib import Path
    import fix_ps1_encoding as fx
    n = 0
    for sub in ("scripts", "hooks", "tools"):
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".ps1"):
                fx.fix(Path(os.path.join(d, fn)))     # fix() 要 Path，不能传 str
                n += 1
    return n


def install_skill(dry):
    """把 SKILL.md 放到 ~/.claude/skills/<name>/ 下，Claude Code 才会加载它。

    正文里写的都是相对本项目根目录的路径，所以复制过去之后要在开头补一行
    绝对路径，否则换个工作目录就找不到脚本了。
    """
    if not os.path.exists(SKILL_SRC):
        return "跳过：SKILL.md 不存在"
    with open(SKILL_SRC, encoding="utf-8") as f:
        text = f.read()
    # 在 frontmatter 之后插入项目根目录，方便任何工作目录下都能定位脚本。
    end = text.find("\n---", 4)
    if end < 0:
        return "跳过：SKILL.md 没有合法的 frontmatter"
    end = text.find("\n", end + 1) + 1
    note = ("\n> 本技能的脚本在：`%s`\n> 下面所有相对路径都相对这个目录。\n"
            % ROOT)
    text = text[:end] + note + text[end:]
    if dry:
        return "将写入 %s（%d 字节）" % (SKILL_DST, len(text.encode("utf-8")))
    os.makedirs(SKILL_DST_DIR, exist_ok=True)
    with open(SKILL_DST, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return "已写入 %s" % SKILL_DST


def remove_skill(dry):
    if not os.path.exists(SKILL_DST):
        return "跳过：%s 不存在" % SKILL_DST
    if dry:
        return "将删除 %s" % SKILL_DST
    os.remove(SKILL_DST)
    try:
        os.rmdir(SKILL_DST_DIR)      # 只在空目录时成功，不误删用户别的东西
    except OSError:
        pass
    return "已删除 %s" % SKILL_DST


TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>AI-Sender: flush queued WeChat notifications after the desktop unlocks.</Description>
  </RegistrationInfo>
  <Triggers>
    <SessionStateChangeTrigger>
      <Enabled>true</Enabled>
      <StateChange>SessionUnlock</StateChange>
      <UserId>{user}</UserId>
      <Delay>PT15S</Delay>
    </SessionStateChangeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{ps}</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{notify}" -FlushOnly</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def whoami():
    return "%s\\%s" % (os.environ.get("USERDOMAIN", os.environ.get("COMPUTERNAME", ".")),
                       os.environ.get("USERNAME", ""))


def task_exists():
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                       capture_output=True, text=True)
    return r.returncode == 0


def register_task(dry):
    xml = TASK_XML.format(user=whoami(), ps=PS, notify=NOTIFY)
    if dry:
        print("  [dry-run] 将注册计划任务 %s（触发器：SessionUnlock，延迟 15s）" % TASK_NAME)
        return True
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        # schtasks 要求 XML 声明的编码与实际一致：这里声明 UTF-16，就必须按 UTF-16 写。
        with open(path, "w", encoding="utf-16") as f:
            f.write(xml)
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("  !! 计划任务注册失败: %s" % ((r.stderr or r.stdout).strip()[:300]))
            return False
        print("  已注册计划任务 %s（解锁 15 秒后自动补发队列）" % TASK_NAME)
        return True
    finally:
        os.remove(path)


def remove_task(dry):
    if not task_exists():
        return
    if dry:
        print("  [dry-run] 将删除计划任务 %s" % TASK_NAME)
        return
    r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                       capture_output=True, text=True)
    print("  计划任务已删除" if r.returncode == 0
          else "  !! 删除失败: %s" % ((r.stderr or r.stdout).strip()[:200]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--with-task", action="store_true",
                    help="同时注册解锁后自动补发的计划任务")
    a = ap.parse_args()

    print("技能目录 : %s" % ROOT)
    print("设置文件 : %s" % SETTINGS)

    # config.json 不进版本库（含会话名、开关状态、发送脚本绝对路径），所以新克隆
    # 下来必然没有。照 config.example.json 生成一份，别让别人卡在「缺少文件」上。
    cfg = os.path.join(ROOT, "config.json")
    if not os.path.exists(cfg):
        sample = os.path.join(ROOT, "config.example.json")
        if not os.path.exists(sample):
            print("!! 缺少文件: %s（也没有 config.example.json 可照抄）" % cfg)
            return 1
        if a.dry_run:
            print("配置文件 : 不存在，正式安装时会照 config.example.json 生成")
        else:
            shutil.copyfile(sample, cfg)
            print("配置文件 : 不存在，已照 config.example.json 生成 -> %s" % cfg)
            print("           总开关默认关闭，要发消息得先跑 wx_switch.ps1 -On")

    # config.json 不在这个必需清单里：上面那段要么已经生成好，要么已经报错退出；
    # --dry-run 下它可以合法地还不存在。
    for p in (DISPATCH, NOTIFY):
        if not os.path.exists(p):
            print("!! 缺少文件: %s" % p)
            return 1

    data = load_settings()
    hooks = data.get("hooks") or {}
    removed = strip_ours(hooks)

    if a.uninstall:
        print("卸载：移除 %d 个 hook 条目" % removed)
        if not a.dry_run:
            if removed:
                bak = backup_settings()
                if bak:
                    print("  已备份 -> %s" % bak)
                if hooks:
                    data["hooks"] = hooks
                else:
                    data.pop("hooks", None)
                save_settings(data)
                print("  settings.json 已更新")
        remove_task(a.dry_run)
        print("技能文件 : %s" % remove_skill(a.dry_run))
        print("（config.json / state 未删除，保留你的开关与队列）")
        return 0

    n = normalize_ps1()
    print("已规范化 %d 个 .ps1（CRLF + 单 BOM）" % n)

    if removed:
        print("发现 %d 个旧条目，将替换" % removed)
    for event, groups in desired_hooks().items():
        hooks.setdefault(event, [])
        hooks[event].extend(groups)
        print("  + %s" % event)

    data["hooks"] = hooks
    if a.dry_run:
        print("--- [dry-run] hooks 段将变成 ---")
        print(json.dumps({"hooks": hooks}, ensure_ascii=False, indent=2))
    else:
        bak = backup_settings()
        if bak:
            print("已备份 -> %s" % bak)
        save_settings(data)
        print("settings.json 已更新")

    print("技能文件 : %s" % install_skill(a.dry_run))

    if a.with_task:
        register_task(a.dry_run)
    else:
        print("（未注册解锁补发计划任务，需要就加 --with-task）")

    ok, where = check_sender()
    if ok:
        print("发送技能 : 找到 %s" % where)
    else:
        print("")
        print("!! 找不到发送技能 wechat-send，装完也发不出消息。")
        print("   %s" % where)
        print("   要么把 wechat-send-skill 放到本项目的同级目录，")
        print("   要么把它的 wx_send.ps1 绝对路径填进 config.json 的 sender_script。")

    print("")
    print("总开关默认关闭，现在不会发任何消息。开启：")
    print('  powershell -NoProfile -ExecutionPolicy Bypass -File "%s" -On'
          % os.path.join(ROOT, "scripts", "wx_switch.ps1"))
    # 以前这儿印的是「hooks 改动需要重启 Claude Code 会话才生效」，照旧版文档写的，错了。
    # 2026-09-19 实测：改完 settings.json，当前会话下一个授权框就触发了 hook。
    print("hooks 改动由 Claude Code 的文件监视器热加载，不用重启会话。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
