# request_permissions 的宿主核心修复提案

状态：**仅供代码审查，未编译、未安装到桌面核心，也未完成修改后的运行验收。**
本目录不被技能安装器作为运行文件复制，不代表技能已经修好桌面权限通知。

## 源码定位

读取的官方公开源码版本为 `openai/codex@5c5308fc9a9ee789049d646ef11e5400384b9c6f`。
这不是对本机 `0.155.0-alpha.9.2` 二进制源码版本的证明；它与本机黑盒对照现象一致。

- [session/mod.rs：request_permissions_for_environment](https://github.com/openai/codex/blob/5c5308fc9a9ee789049d646ef11e5400384b9c6f/codex-rs/core/src/session/mod.rs#L2928) 直接调用 `request_guardian_approval`。无自动审查决定时，注册待处理权限请求并发送 `EventMsg::RequestPermissions`，中间没有调用权限 Hook。
- [tools/approvals.rs：公共 request_approval](https://github.com/openai/codex/blob/5c5308fc9a9ee789049d646ef11e5400384b9c6f/codex-rs/core/src/tools/approvals.rs#L503) 才调用 `run_permission_request_hooks`。
- 同一文件的 [RequestPermissions 分支](https://github.com/openai/codex/blob/5c5308fc9a9ee789049d646ef11e5400384b9c6f/codex-rs/core/src/tools/approvals.rs#L861) 明确假定这类请求直接走 Guardian，因此不能简单把前一个调用改成 `request_approval`，否则人工分支会触发 `unreachable!`。

## 候选改法

`request-permissions-hook.patch` 仅修改 `request_permissions_for_environment`：

1. 保留原有 Never / granular 权限策略检查。
2. 自动审查得到决定时直接按原逻辑结束，不触发权限通知。
3. 只有自动审查没有处理、即将进入人工审批时，调用现有受信任的 `PermissionRequest` Hook。
4. Hook 的 Allow / Deny 仍使用原有响应规范化和权限记录逻辑；Hook 未决定则进入原来的人工审批。
5. 等待 Hook 时尊重取消；不修改信任记录、不默认批准权限。

该顺序是针对“自动审批不用提醒”的提案。公共审批入口当前采用 Hooks → Guardian/User 的顺序，
因此此处改变的优先级需要宿主维护者明确审查，不能把它包装成已经获认可的上游修复。
多个 Hook 自行处理请求时，通知型 Hook 仍不能保证一定出现人工等待；这一点与已有 PermissionRequest 语义相同。

## 已验证与未验证

- 已验证补丁可在上述固定源码文件上通过 `git apply --check`。
- 本机没有可用的 Rust 工具链；尚未编译，未执行 Rust 单元测试。
- 桌面仍使用原有官方核心，没有替换或注入桌面进程。
- 官方 App Server 协议有 `item/permissions/requestApproval`，但需要承载任务的实时连接。当前桌面核心使用 stdio，未发现可供旁路观察的本机 TCP 监听；默认控制 socket 不存在，官方 `app-server proxy` 在 Windows 本机返回错误 10050。
- 历史数据库不提供可可靠关联的“正在等待人工权限审批”状态。不能用日志中出现工具名、等待时长或另开 App Server 的 `notLoaded` 状态代替真实事件。

安装到真实桌面前，至少需要完成：匹配桌面版本的源码构建；人工请求触发一次；自动允许/拒绝不通知；
Never/granular 拒绝不通知；取消、Hook 无决定/允许/拒绝的回归；手机端收件确认。
当前项目的 `python tools/probe_codex_approvals.py --codex-bin <候选核心路径> --output <报告路径>` 可用于候选核心的第一轮对照，
仍须继续完成桌面连接的真实验收。

## 为什么没有直接替换桌面核心

技能修改的是通知脚本，缺失调用发生在 Codex 宿主。当前没有验证过的、与桌面版本匹配的候选二进制，
也没有确认过的桌面自定义核心接入方式。替换已安装的官方核心既不能算技能安装，也不能凭源码补丁认定兼容。
本提案用于继续修复和上游沟通，不声称已让本机桌面这一路生效。
