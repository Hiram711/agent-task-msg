# Codex 适配（Windows）

## 支持范围

`PermissionRequest` → `needs_input`：Codex 准备请求权限批准时，后台通知本机微信。
标题显示 `[Codex] 需要你处理`，正文包含时间、项目、工具名，以及事件提供的审批说明。
不会转发完整命令、MCP 参数或聊天记录。审批说明自身可能含敏感内容，使用前应确认目标会话。
适配器不输出任何审批决定，也不自动批准或拒绝请求。

当前官方事件列表没有 Claude Code 的 `Notification` 和 `StopFailure`，因此：

- `triggers.error` 对 Codex 没有自动触发作用。
- 普通提问、MCP 引导输入不保证会触发 `PermissionRequest`，不承诺覆盖所有“等待输入”。
- 不把 `Stop` 当作 API 错误或任务成功，也不注册完成通知。用户要求收尾发微信时，由智能体调用 `wechat-send`。
- 不解析不稳定的 Codex 内部日志，不通过定时轮询猜测会话状态。

依据：[Codex Hooks 官方文档](https://developers.openai.com/zh-Hans/docs/hooks)（核对于 2026-09-20）。
本地开发验证版本为 Codex CLI 0.154.0，`hooks` 功能已启用；桌面版实际审批触发需要另行实测。

**本机实测限制**：微信发送链路已通过，但桌面版核心 0.155.0-alpha.9.2 下，hook 已受信任，
重启后本任务的 `request_permissions` 申请仍未产生通知。当前不能宣称桌面端自动审批提醒已经可用。
详见 [联调记录](integration-result.md)。

## 安装

需要 Windows、Python 3.9+、Windows PowerShell 5.1、支持 hooks 的 Codex，以及已登录的本机微信。
先把 `wechat-send-skill` 放在 Codex 技能目录中；也支持目录名 `wechat-send`。
两个仓库并排放但尚未安装发送器时，用 `--sender-script` 指定发送脚本绝对路径。

在本仓库根目录运行：

```powershell
python tools/install_codex.py --dry-run
python tools/install_codex.py
```

可选参数：

```powershell
python tools/install_codex.py --codex-home 'D:\CodexHome' --dry-run
python tools/install_codex.py --sender-script 'D:\skills\wechat-send-skill\scripts\wx_send.ps1'
```

安装器将完整技能复制到 `<CodexHome>/skills/agent-task-msg`，hook 指向该目录。
安装后不依赖克隆仓库的位置，可以从任意工作目录调用。
默认使用环境变量 `CODEX_HOME`，未设置时使用 `~/.codex`。

- 首次安装由 `config.example.json` 生成 `config.json`，推送默认关闭。
- 更新保留现有 `config.json`、`state/` 和其它 hook；显式指定 `--sender-script` 才更改发送器路径。
- 修改已有文件前创建同目录 `.bak-时间戳` 备份；通过临时文件替换单个文件，最后更新 `hooks.json`。
- `--dry-run` 完全不写文件，包括配置、脚本编码和目录。
- 重复安装不会叠加 hook。移除时按专用 `HookOwner` 标记识别 handler，同一组里的其它 handler 保留。
- 不修改 `~/.claude/settings.json`、`config.toml`、hook 信任记录，也不注册 Windows 计划任务。

命令使用 PowerShell `-EncodedCommand`，避免路径中的空格、中文、引号或 shell 特殊字符导致错误。
其中只有安装路径和固定参数，解码后的形式是：

```powershell
& '<CodexHome>\skills\agent-task-msg\hooks\dispatch.ps1' -Agent Codex -Kind needs_input -HookOwner agent-task-msg-codex
```

安装后，在 Codex 审查并信任该 hook（CLI 使用 `/hooks`）。若当前客户端尚未显示新配置，重新打开会话再检查。
不要修改信任记录或使用跳过信任检查的参数。若管理员禁用了 hooks，应遵循管理员设置。
确认用户要求开启后，在**安装目录**调用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File '<CodexHome>\skills\agent-task-msg\scripts\wx_switch.ps1' -Status
powershell -NoProfile -ExecutionPolicy Bypass -File '<CodexHome>\skills\agent-task-msg\scripts\wx_switch.ps1' -On
```

状态中的开关只反映发送配置，不证明 Codex 已加载或信任 hook。
`error` 开关仅供 Claude Code 使用。

## 验证与排查

不发微信的自动化回归测试：

```powershell
python -m unittest discover -s tests -v
```

测试在独立临时目录安装，使用假的发送器和后台接收器；不会修改真实 Codex 配置或操作微信。
覆盖安装幂等性、混合 hook 保留、卸载、只读预览、UTF-8/BOM、不同审批事件的去重及 Claude 标题兼容。
仓库原来的 `tools/test_notify.ps1` **会真发微信**，不要把它当作无副作用测试执行。

实际链路需要用户信任 hook、开启推送、登录微信，并在 Codex 产生一次真正需要批准的操作。
不应为了制造审批而执行危险命令。默认键鼠空闲不足 120 秒时不发送；手工测试时可以暂时将
安装目录配置的 `presence_idle_min_seconds` 改为 `0`，测试后恢复。
先确认 hook 已执行，再查安装目录下的 `state/notify.log` 和 `wx_switch.ps1 -Status`。
锁屏或抢不到前台时，仍按原配置默认丢弃通知；不会使用外部推送服务绕过。

## 移除 Codex hook

```powershell
python tools/install_codex.py --uninstall --dry-run
python tools/install_codex.py --uninstall
```

只移除本适配器注册的 hook，保留技能文件、配置、队列和备份。
从已安装目录运行 `tools/install_codex.py` 也可更新或移除 hook。
