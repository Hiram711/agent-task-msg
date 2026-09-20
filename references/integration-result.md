# Codex 联调结果

验证日期：2026-09-20。环境：Windows、Windows PowerShell 5.1、Codex CLI 0.154.0，以及桌面版内置核心 0.155.0-alpha.9.2。

## 已通过

- 安装器将运行文件复制到 `$CODEX_HOME/skills/agent-task-msg`，并合并 `$CODEX_HOME/hooks.json`。
- Codex 的 `hooks/list` 能识别 `permissionRequest`，无配置错误或警告；用户审查后状态为 `trusted`。
- 向已注册命令注入中文审批事件，完整走通 `dispatch → wx_notify → wx_send`。
- 发送器 OCR 核对目标为「文件传输助手」，确认输入框有字且按回车后清空，返回成功。消息区 OCR 未读到标题，因此未独立验证手机端收件。
- 13 项离线回归测试通过，包括中文/BOM、空参数、特殊字符路径、去重、安装幂等性及其它 hook 的保留。
- 测试完成后恢复原配置，推送关闭、空闲阈值 120 秒；无测试截图遗留。

## 尚未接通：桌面端自动审批通知

hook 已受信任、推送临时开启且空闲判断关闭时，当前桌面任务的提权请求和 `request_permissions` 网络权限申请均未产生新通知日志或 payload。重启后复测结果相同。

因此，**手工事件注入成功不能视为桌面端自动审批提醒成功**。官方文档描述的 shell escalation / managed-network approval 不足以证明所有客户端的 `request_permissions` 路径均会触发该事件。

目前尚不能仅凭无日志区分「事件未派发」和「宿主未运行命令」。需要进一步定位宿主执行路径；重复重启不能解决已观察到的问题。

## 桌面访问

在受限 shell 中运行微信诊断时，可能无法访问真实桌面（前台句柄为零、截图失败）；经正常权限审批后在真实桌面运行同一诊断成功，微信窗口和中文 OCR 可用。

整个测试没有改写 hook 信任记录、跳过信任检查或长期打开推送。
