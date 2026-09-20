# Codex 适配（Windows）

## 三条通知入口

| 场景 | 实现 | 前提 |
| --- | --- | --- |
| 权限审批 | `PermissionRequest` hook | 需宿主派发事件并信任 hook；当前桌面路径仍有实测限制 |
| API 等错误导致回合失败 | 独立的 `codex_watch.py` 读取持久化回合状态 | 当前任务观察器已运行；仅对明确的 `failed` 提醒 |
| 等待用户回答 | 智能体提问前调用 `codex_notify.py` | 技能已加载且执行了提问流程；不是全局事件监听 |

不注册任务完成通知；`completed`、`interrupted`、重试中及读取错误均不当作 API 故障。
两个新入口都遵守总开关、目标会话、免打扰、队列与去重配置，不自动批准权限。

依据：[官方 Hooks](https://developers.openai.com/zh-Hans/docs/hooks)、[官方 App Server](https://developers.openai.com/zh-Hans/docs/app-server)（2026-09-20 核对）。

## 安装与开启

需要 Windows、Python 3.9+、Windows PowerShell 5.1、Codex 和已登录的微信。
先把 `wechat-send-skill`（或 `wechat-send`）安装到 Codex 技能目录。

```powershell
python tools/install_codex.py --dry-run
python tools/install_codex.py
```

安装器将完整技能放入 `$CODEX_HOME/skills/agent-task-msg`，合并 `$CODEX_HOME/hooks.json`。
未设置环境变量时，`CODEX_HOME` 默认为 `~/.codex`。可以用 `--codex-home` 指定安装位置，
用 `--sender-script` 显式指定 `wx_send.ps1`。现有配置、状态和其它 hook 会保留；旧配置缺少
`triggers.question` 时按 true 处理，但总开关仍必须开启。

安装器在修改文件前创建 `.bak-时间戳`，重复安装不叠加 hook；`--dry-run` 不写任何文件。
它不修改 hook 信任记录、不注册系统计划任务，也不自动打开推送或启动观察器。
更新时应先停止旧观察器，更新后按需重新启动，避免旧进程继续运行旧代码。

在**已安装的技能根目录**运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/wx_switch.ps1 -On
python scripts/codex_watch.py start
python scripts/codex_watch.py status
```

观察器默认使用 `CODEX_THREAD_ID`，缺失时用 `--thread-id <已确认的任务ID>`。
它只观察这个任务，不覆盖用户的所有任务。必须先看到 `state: running`，才算已经建立观察基线。
`start` 输出 launched 只表示进程已启动；`degraded` 或 `stale: true` 表示需要排查。
需要指定与桌面版相符的核心时使用 `--codex-bin 'C:\path\codex.exe'`。
权限/桌面访问按环境正常审批，不应关闭沙箱或绕过信任检查。

关闭：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/wx_switch.ps1 -Off
python scripts/codex_watch.py stop
```

`stop` 使用停止标记，不按 PID 强杀进程；已经进入发送流程的消息可能先完成。
进程崩溃后不会自动重新启动；再次 `start` 复用检查点。它不是 Windows 开机服务。

## 错误通知如何判断

观察器每隔约 5 秒通过独立 App Server 调用 `thread/read` 和 `thread/turns/list`，
使用 `itemsView: notLoaded`，不读取聊天正文、不解析内部日志、不恢复或修改任务。
该进程独立于模型，所以模型的 API 回合失败后仍能继续检查。

首次快照建立基线，不补报启动前的历史失败。之后仅通知新出现或由运行状态变成 `failed` 的回合，
检查点避免重启重报。总开关关闭期间观察到的失败也会被记住，不在重新开启时补发旧错误。
不转发上游原始错误正文，只发送错误类型，避免上游回显的凭据等内容进入微信。

限制：`thread/turns/list` 仍是实验接口；当前版本、存储格式或权限不支持时观察器会报告异常。
回合必须被 Codex 持久化为 `failed`；程序被强杀、操作系统关机或仅记录为 `interrupted` 时不会猜测故障。
极新的空任务在首次回合写入前可能尚不可读，应等 `check` / `status` 确认成功。
网络重试、观察器断连和正常结束不会发送错误提醒。

```powershell
python scripts/codex_watch.py check
python scripts/codex_watch.py run --max-seconds 15
```

`run` 在前台观察，`start` 后台观察；`--max-seconds` 可用于有界诊断。多个相同任务的观察器由文件锁避免重复执行。
状态和检查点位于技能目录下 `state/codex-watch/`，与 `notify.log` 一样不进入版本库。

## 提问前主动通知

在 Codex 停下来等待必要回答之前，将问题摘要写入 UTF-8 文件，然后调用：

```powershell
python scripts/codex_notify.py --message-file 'C:\work\question.txt'
```

消息标题为 `[Codex] 等待你回答`。可以提供 `--thread-id`、`--turn-id` 和 `--cwd`；
缺省任务 ID 来自 `CODEX_THREAD_ID`。正文走文件，不拼入命令行。
调用结束后再展示问题；即使通知失败，也要正常展示问题，不无限重试。
返回 `disabled` 表示开关关闭；`processed` 只表示流程已处理，可能发送、跳过或排队，需看日志。

该流程能处理普通文字提问和问题卡片，但依赖智能体执行技能步骤。
没有加载技能、没有调用入口或事先因 API 故障停止时，不能保证提问提醒。

## 权限 hook 与微信发送限制

新权限 hook 仍需用户在 `/hooks` 中审查并信任。它使用 `-EncodedCommand` 处理带空格和特殊字符的路径；
解码形式为：

```powershell
& '<CodexHome>\skills\agent-task-msg\hooks\dispatch.ps1' -Agent Codex -Kind needs_input -HookOwner agent-task-msg-codex
```

本机桌面版核心 0.155.0-alpha.9.2 的 `request_permissions` 路径实测未产生通知，
重启也未解决。两个新增入口不依赖这个权限 hook，但不代表权限审批问题已修复。
微信仍需要可操作的桌面；锁屏、抢不到前台等情况按原配置处理。
默认键鼠空闲不足 120 秒时不发送，检查开关用 `wx_switch.ps1 -Status`，查发送结果看 `state/notify.log`。

## 测试

常规回归测试使用替身发送器，不操作微信：

```powershell
python -m unittest discover -s tests -v
```

可选的真实 Codex 集成测试在临时 `CODEX_HOME` 中连接本机 HTTP 500 模拟服务，验证独立观察器和通知脚本，
使用替身微信发送器；不需要 API 密钥，不请求外部模型：

```powershell
$env:CODEX_TEST_BIN = 'C:\path\codex.exe'
python -m unittest discover -s tests -p test_codex_failure_integration.py -v
```

原来的 `tools/test_notify.ps1` 会真发微信，不属于上述无微信测试。

## 移除

先停止当前任务的观察器，再移除权限 hook：

```powershell
python scripts/codex_watch.py stop
python tools/install_codex.py --uninstall
```

卸载入口只移除自己注册的 hook，保留技能文件、配置、队列和备份。
