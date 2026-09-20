---
name: agent-task-msg
description: 通过本机微信提醒 Codex 回合失败、等待用户回答及权限审批，或 Claude Code 的待确认和 API 错误中断。用于开启、关闭推送和查询状态；Codex 错误提醒需独立观察器，提问及人工权限申请前由智能体主动通知，自动审查模式跳过权限预提醒。任务完成通知仍按用户要求调用 wechat-send。
---

# 微信任务提醒

任务状态变化时，往本机微信「文件传输助手」发一条中文提醒。

## 运行环境

- **Codex**：先读 [Codex 安装与事件说明](references/codex.md)，使用 `tools/install_codex.py`。安装器复制完整运行文件到 `$CODEX_HOME/skills/agent-task-msg`，并合并 `$CODEX_HOME/hooks.json`；默认 `CODEX_HOME` 是 `~/.codex`。权限 hook、错误观察器、主动提问和权限申请预提醒是独立入口。新 hook 需要用户审查并信任，安装技能本身不代表自动通知已接通。
- **Claude Code**：使用 `tools/install.py`；下面的 `Notification` / `StopFailure` 和 `settings.json` 热加载说明仅适用于 Claude Code。
- 两种环境共用下述开关、免打扰、队列与微信发送逻辑。Codex 安装版的相对路径以已安装的技能目录为准，不要误改克隆仓库的 `config.json`。

设计前提有两条，别绕开：

1. **不出本机。** 不用企业微信机器人、不用服务号/PushPlus 之类的推送网关。代价是发送瞬间会抢一下焦点 —— 这是用户明确要求的（「立即发，不管打扰」）。
2. **默认关闭。** `config.json` 里 `enabled` 默认 `false`，字段缺失也按 `false` 算。用户没说开，就一条都不发。

**真正操作微信客户端的那一层是独立技能 `wechat-send`**，默认在本项目的同级目录 `wechat-send-skill`。本技能只负责"什么时候该发、发什么内容"，不含任何 GUI 自动化代码。前台化、OCR、探针校验、退出码含义、锁屏探测、排查工具全在那边，改那些先去那边看。

## 用户让我开关或查状态时

别自己去改 `config.json`，用这个脚本，它会顺带校验字段：

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/wx_switch.ps1 -Status
```

| 用户的话 | 执行 |
| --- | --- |
| 开启推送 / 开始提醒我 | `-On` |
| 关掉推送 / 别提醒了 | `-Off` |
| 现在是开还是关 / 队列里有几条 | `-Status` |
| 发到某个别的聊天窗口 | `-Target "<会话名>"` |

`-Status` 会打印总开关、目标会话、触发器开关、发送脚本位置、待补发队列条数。其它字段（在场阈值、去重窗口、要不要排队）没有命令行开关，直接改 `config.json`。

**在 Codex 中开启推送时，还要启动当前任务的错误观察器**：`python scripts/codex_watch.py start`。
它从 `CODEX_THREAD_ID` 获取当前任务；环境变量不存在时，传入已确认的 `--thread-id`，不要猜 ID。
用 `python scripts/codex_watch.py status` 确认 `running`，不能把启动命令成功等同于已经监测。
关闭时运行 `wx_switch.ps1 -Off`，然后 `python scripts/codex_watch.py stop`。
观察器每个任务单独启动，不会自动监测其它任务。权限/执行环境不允许启动时，明确报告错误观察尚未启用。

**Codex 调用 `request_permissions` 前，先按当前生效的审批模式过滤。**

- `approvals_reviewer = auto_review`（旧名 `guardian_subagent`）：直接申请，不发权限预提醒；通过后继续。被拒绝时遵守审查结果，有安全替代方案就继续，确实需要用户决定时走下述提问通知。
- 明确为 `user`，且确实即将申请尚未获得的权限：将简短用途写入 UTF-8 文件，运行 `python scripts/codex_notify.py --kind permission_request --approvals-reviewer user --message-file <绝对路径>`，然后再调用原权限工具。
- 模式不明，或已知策略禁止该申请：不猜测人工等待、不发送预提醒。已有权限足够时不再申请。

模式以当前任务/回合的有效上下文为准，不用全局 `config.toml` 的默认值覆盖它。脚本缺少模式参数也会跳过。
这是申请前的模式过滤，不是预测批准结果；标题为“即将申请访问权限”，不能声称审批卡片已出现。
只用于 `request_permissions`，不扩展到每次文件操作或 `exec_command` 提权；已有命令审批 hook 保持原流程。
入口依赖技能已加载且智能体执行步骤，不是全局监听，也不修改 Codex 启动器或核心。
通知关闭、跳过或失败都不阻止正常权限申请；不重复通知，也不为了发送预提醒递归申请权限。

**在 Codex 中准备停下来等用户回答时**（包括 `request_user_input`、异步问题卡片或最终回复中的必要问题）：
将真正需要用户决定的问题和简短选项写入 UTF-8 文件，然后运行
`python scripts/codex_notify.py --message-file <绝对路径>`，再发出问题。
只通知明确需要回答的问题，不把修辞问句或普通结束当作等待输入。
通知失败也要把问题正常展示出来，不要陷入重复通知或自动重试；不要在微信正文里放凭据、完整命令或敏感上下文。
该入口遵守总开关、`triggers.question` 和免打扰设置，关闭时会直接跳过。

开关修改不用重启会话：`wx_notify.ps1` 每次触发都重新读 `config.json`。Claude Code 的 hook 配置由文件监视器热加载；Codex 的 hook 修改需重新审查其信任状态，不能套用 Claude 的热加载结论。

## 触发器

以下是 Claude Code 的事件。Codex 的 `PermissionRequest` 映射到 `needs_input`；
独立观察器将明确的 `failed` 回合映射到 `error`；主动提问入口使用 `question`，人工模式权限预提醒使用 `permission_request`。
Codex 的权限 hook 在本机桌面审批路径仍有实测限制，见 [联调记录](references/integration-result.md)。

| 触发器 | 什么时候响 | 对应 hook | 默认 |
| --- | --- | --- | --- |
| `needs_input` | 要用户确认/授权，或子 agent 等输入 | `Notification`（matcher：`permission_prompt`、`agent_needs_input`、`elicitation_dialog`、`elicitation_url_dialog`） | 开 |
| `error` | 回合因 API 错误结束 | `StopFailure` | 开 |

Claude hook 仍只有这两个，没有「任务完成」那一路。任务完成时智能体还能调工具，用户交代「完事微信叫我」时自己调 `wechat-send`，不注册 `Stop` / `UserPromptSubmit` 完成通知。

### 听到「完事微信叫我」，先回一句确认

上面那条设计赌的是「Claude 还活着就会记得自己发」。2026-09-19 一晚上赌输三次，于是用户加了这条约定。

**收到这类交代，当场在回复里写一句「记下了，收尾时发微信」，同时写进 todo。** 收尾前扫一遍 todo，再决定能不能说「做完了」。

理由是漏的形状很固定：**「等你干完了再做 X」**。读到的那一刻它被一句「等收尾时办」打发掉，而收尾在几十个工具调用之后，中间没有任何载体带着它。把确认写进自己的输出，它就成了记录的一部分，比只待在脑子里扛得住上下文压缩 —— 那天丢掉的其中一条（用户给的 GitHub 仓库地址）正是被压缩吃掉的。

另外两点同样别忘：

- **问句和指令混在一句里最容易漏。** 栽的那条原话是「现在这个技能开着不，我去睡觉了，任务完成用微信通知我呢」—— 前半句是问句，后半句是派活，我答了前半句就往下干活了。回话前多问自己一句：这条除了在问我什么，有没有同时在派活。
- **「我去睡觉了」这类话要顶格对待。** 它把「发不发那条微信」从锦上添花变成这一整轮的交付物。

**这两条为什么替代不了。** `needs_input`：等你批准工具调用时 Claude 是被挂起的，调不了任何东西，只能靠 hook 替它喊。它**本可以**在动手前先自己发一条，但弹不弹窗取决于权限模式、allow/deny 规则和会话内的「不再询问」，预测错了是静默的 —— 你不知道该回来，就一直等着。这条兜的就是预测错的那些。`error`：回合崩了 Claude 就没了，发不了。

`StopFailure` 是**每回合最多一次**，重试成功不算 —— 所以它响了就是真停住了，不会在可恢复的抖动上吵你。matcher 留空表示所有错误类型都报。

matcher 里刻意不含 `idle_prompt`（「一轮干完在等下一句」）—— 那和 Claude 自己发的「干完了」是同一件事。

之上还有一道**在场判断**：`GetLastInputInfo` 读键鼠空闲时长，不到 `presence_idle_min_seconds` 就不发。理由是发一条要夺前台约 40 秒并把微信整个窗口拉到最上层，你正盯着屏幕时这只是打断。判断不出来时按「不在场」处理，照发。

这道闸也挡补发：人在跟前时连积压的消息都不抢前台。手动 `-FlushOnly` 例外，那是你自己叫的。

要**手工验证触发器**，先把 `enabled` 开着、`presence_idle_min_seconds` 临时置 `0`，否则会静默失败、看着像 hook 没生效：在场判断是在 hook 触发的那一刻读空闲时长的，而你手工造触发条件就得敲键盘，读到一两秒必然被拦下。

消息格式是「带摘要」：标题行 + 时间 + 项目，再跟一段消息正文的节选（截断到 `summary_max_chars`）。

> 以前这儿还有一套回合计时（`state/turn_<session_id>.txt`、`Get-Elapsed`、正文里的「耗时」行），只给已删掉的「任务完成」那一路筛短回合用。2026-09-19 一并删净了 —— 写它的 `UserPromptSubmit` hook 早就不注册，日志里一直是「耗时 未知」。别照着这段描述把它加回来。

## 配置

`config.json`，UTF-8：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 总开关。`false` 或字段缺失都不发。 |
| `target` | `文件传输助手` | 发给哪个会话。改之前先确认微信侧栏里能搜到这个名字。 |
| `sender_script` | `""` | `wechat-send` 的 `wx_send.ps1` 绝对路径。留空就按默认位置找（见「文件」一节）。发送技能装在别处时填这个。 |
| `triggers.needs_input` | `true` | 需要确认/授权时是否发。 |
| `triggers.error` | `true` | 出错中断时是否发。 |
| `triggers.question` | `true` | Codex 主动提问前是否发；旧配置缺字段也按 true，仍受总开关控制。 |
| `triggers.permission_request` | `true` | Codex 人工模式的权限申请预提醒；缺字段时沿用 `needs_input`，后者也缺失则 true。自动审查或模式不明时仍跳过。 |
| `presence_idle_min_seconds` | `120` | 键鼠空闲不到这个数就不发 —— 你人在机器前，该看见的已经看见了，没必要为此夺走前台 40 秒。设 `0` 关掉这道判断（回到「立即发，不管打扰」）。 |
| `summary_max_chars` | `220` | 摘要截断长度。 |
| `dedupe_seconds` | `90` | 这个窗口内同样内容只发一条。去重键刻意**不含**时间那一行，否则等于永不去重。 |
| `queue_when_unreachable` | `false` | 桌面这会儿够不着时（锁屏 exit 8、抢不到前台 exit 3）是否转队列。默认**直接丢弃** —— 这类提醒指向某个具体时刻（现在要你批准／刚崩了），过了那个点补一条只是噪音。改 `true` 则转队列等下次触发补发。旧名 `queue_when_locked` 仍然认（那时只管锁屏一条）。 |
| `queue_max_age_hours` | `24` | 队列里积压超过这个时长的消息直接丢弃，不再补发。过期的「需要你处理」只是噪音 —— 它指的那个回合早就过去了。设 `0` 表示永不过期。 |
| `log_max_kb` | `512` | `state/notify.log` 超过就轮转。 |

## Claude Code 安装

Codex 请使用 [独立安装入口](references/codex.md)，不要运行本节的 Claude 安装器。

**先装 `wechat-send`**，本技能靠它发消息。它自己的安装器不碰 `settings.json`：

```bash
python ../wechat-send-skill/tools/install_skill.py --dry-run
```

然后装这个：

```bash
python tools/install.py --dry-run
```

看清楚它要往 `~/.claude/settings.json` 里加什么，再去掉 `--dry-run` 真跑。它会：

- 先备份 `settings.json`；
- 注册 `Notification` 和 `StopFailure` 两个 hook，都指向 `hooks/dispatch.ps1`，条目用 `ai-sender-skill` 标记，便于 `--uninstall` 精确摘除。同时会把以前注册过的 `Stop` / `UserPromptSubmit` 条目清掉（`strip_ours` 扫的是 settings 里现有的事件键，不是当前想要的那几个），你自己的其它 hook 不动；
- 规范化所有 `.ps1` 的编码（CRLF + 单个 UTF-8 BOM，PowerShell 5.1 的硬要求）；
- 带 `--with-task` 时再建一个 `AI-Sender-FlushOnUnlock` 计划任务（`SessionUnlock` 触发，延迟 15 秒），用来解锁后补发队列。

**装完不用重启会话。** 2026-09-19 实测：`install.py` 改完 `settings.json`，当前会话下一个授权框就触发了 hook。官方文档也是这么说的 ——「Direct edits to hooks in settings files are normally picked up automatically by the file watcher」，另有 `ConfigChange` 事件专门对应配置文件中途改变。本文档和 `install.py` 以前都说要重启，那是照旧版文档写的，错了。`/hooks` 现在只是只读查看器，不是「审核改动使其生效」的步骤。

卸载：`python tools/install.py --uninstall`。

## 已知限制：桌面够不着的时候发不出去，而且不补

这条得说清楚，因为它正好落在这个 skill 最该起作用的场景上。

屏幕锁了（或者息屏）之后，Windows 不给任何程序操作桌面：`SetForegroundWindow` 失败、截屏全黑、合成的鼠标键盘落不到任何窗口上。也就是说**人离开电脑的时候，恰恰是微信自动化唯一做不到的时候**。在「消息不出本机」这个前提下没有绕路 —— 绕路就意味着走第三方推送，那是用户明确拒绝的。

**这种情况下的提醒直接丢，不排队。** 用户 2026-09-19 定的（`queue_when_unreachable: false`），原话「没必要，直接丢弃即可，等我回来再触发已经不需要了」。道理是留下的两种提醒都指向某个具体时刻 —— 「现在要你批准」和「刚崩了」—— 过了那个点补一条微信只是噪音。

有两个退出码算「桌面够不着」，处理一样：

| 码 | 什么情况 | 为什么补发没意义 |
| --- | --- | --- |
| 8 | 锁屏／息屏 | 你解锁回来时，该批的早卡在那儿摆着，该崩的也崩完了 |
| 3 | 抢不到前台（你正在用电脑，手上的程序跟脚本争焦点） | 你人就在跟前，屏幕上都看得见 |

两种都是**消息没发出、也没乱按**，不是脚本坏了。

队列机制本身还留着，给真正可能是暂时性故障的失败用（微信窗口没开、会话没定位到、输入框没找着）。把 `queue_when_unreachable` 改回 `true` 就恢复这两条路的排队。那条路上还有：队列上限 30 条超了丢最旧的；补发的消息带 `[补发 MM-dd HH:mm]` 前缀；一条发 5 次不成就丢；积压超过 `queue_max_age_hours` 也丢；**关掉总开关时连补发也不走**（`-Status` 会额外提示一行，免得看到「待补发 N 条」以为它们迟早会自己出去）。`--with-task` 装的 `AI-Sender-FlushOnUnlock` 计划任务也只对这条路有意义。

## 退出码怎么处理

退出码的完整含义在 `wechat-send` 那边。本技能只区分三种处理：

| 码 | 处理 |
| --- | --- |
| 0 | 记一行"发送成功"，队列里那条删掉 |
| 3、8 | 桌面够不着（抢不到前台／锁屏）。**默认直接丢弃**，`queue_when_unreachable` 改 `true` 才转队列 |
| 其他 | 转队列重试，同一条攒到 5 次失败就放弃删掉 |

补发时碰上 3 或 8 会**停止补发、队列原样保留，且不计入那条消息的失败次数** —— 桌面够不着跟这条消息本身没关系，白扣一次会让它提前被 5 次上限丢掉。

**找不到发送脚本不走队列。** 那是配置错误不是"这次发不出去"，排队只会每次触发都重试、每次都失败，最后按 5 次上限丢掉，还把真正的原因埋在一堆"发送失败"里。日志会直接写清该怎么修。

发送侧本身怎么排查（诊断、屏幕探针、OCR 探针、截图清理），看 `wechat-send` 的 SKILL.md。

自测整条通知链路（会临时改 `config.json` 和去重记录，跑完原样还原）：

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File tools/test_notify.ps1
```

## 文件

| 路径 | 作用 |
| --- | --- |
| `hooks/dispatch.ps1` | hook 入口。必须立刻返回、绝不往 stdout 写东西。自己读 stdin 原始字节再按 UTF-8 解码（本机控制台是 CP936，直接 `ReadToEnd` 会把 BOM 和开头的 `{` 一起吃成乱码）。 |
| `state/dispatch.jsonl` | 不含原始参数的入口阶段日志；推送关闭也记录，排查方式见 Codex 说明。 |
| `tools/probe_codex_approvals.py` | 使用本机模拟模型，对照命令提权与权限申请；临时会话中的审批全部拒绝，不发送微信。 |
| `tools/install_codex.py` | Codex 安装、预览和移除 hook。保留用户配置与其它 hook。 |
| `scripts/codex_watch.py` | 独立观察当前任务的持久化 failed 回合；支持 check/start/status/stop。 |
| `scripts/codex_notify.py` | Codex 提问及人工模式权限申请前的主动通知入口。 |
| `scripts/codex_rpc.py` | 官方 App Server 只读客户端，不启动或恢复任务。 |
| `references/codex.md` | Codex 事件范围、信任步骤、安装和无微信自测。 |
| `scripts/wx_notify.ps1` | 判断该不该发、组装正文、去重，然后调发送技能或排队。 |
| `scripts/wx_switch.ps1` | 开关与配置。 |
| `state/` | 去重记录、队列、日志。可随时删，会重建。 |
| `tools/test_notify.ps1` | 自测。会强制打开两个触发器（`Set-Triggers`）和 `queue_when_unreachable`（`Set-QueueUnreach`）以覆盖所有路径，跑完从 `$cfgBak` 整体还原 —— 所以日常配置里 `queue_when_unreachable` 是 `false` 不影响自测。强制排队还让它在锁屏下也能全跑（走排队路径而不是丢弃）。 |

发送脚本不在这个项目里，在 `wechat-send` 技能：

| 找法 | 顺序 |
| --- | --- |
| `config.json` 的 `sender_script` | 最高。填了就只认它 —— 填了却找不到是配置错误，不会悄悄退回默认路径，否则你以为在用自己指定的那份，实际用的是别的。 |
| `../wechat-send-skill/scripts/wx_send.ps1` | 默认布局，两个技能并排放。 |
| `../wechat-send/scripts/wx_send.ps1` | 按技能名安装发送器时的布局。 |
| `scripts/wx_send.ps1` | 拆分之前的老布局，留着兼容。 |

一条都找不到就跳过本次并在日志里说明，不排队（理由见上一节）。
