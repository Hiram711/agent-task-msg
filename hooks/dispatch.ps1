<#
.SYNOPSIS
    AI-Sender hook 入口。读 stdin 的 hook JSON，落盘，然后把真正的活儿丢给后台进程。

.DESCRIPTION
    hook 是同步阻塞的：微信自动化要十几秒，绝不能在 hook 里等。
    所以这里只做两件极快的事，然后立刻退出：把 payload 写到临时文件，spawn 一个
    隐藏的 powershell 跑 wx_notify.ps1，不等它。

    永远 exit 0、永远不往 stdout 写东西：hook 的 stdout 会被当成上下文注入会话。

.PARAMETER Kind
    needs_input | error。留空曾经表示"只记录回合起点"，那条路随 long_task_done
    一起删了（2026-09-19）；现在留空就是啥也不干。
#>
param(
    [string]$Kind = '',
    [ValidateSet('Claude Code', 'Codex')]
    [string]$Agent = 'Claude Code',
    # Codex 安装器用这个稳定标记更新/卸载自己的 handler，不靠仓库目录名。
    [ValidateSet('', 'agent-task-msg-codex')]
    [string]$HookOwner = ''
)

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

try {
    # 必须自己读字节再按 UTF-8 解码，不能用 [Console]::In.ReadToEnd()：
    # 本机控制台的 InputEncoding 是 CP936，而 hook 的 JSON 是 UTF-8，实测
    # 开头的 BOM "EF BB BF 7B" 会被解成「锘縶」，连 '{' 都被吃进乱码里，
    # 落盘的 payload 就成了废文件：wx_notify.ps1 那边 ConvertFrom-Json 直接失败，
    # 项目名和中文摘要全丢，只剩一条没有正文的干提醒。
    $raw = ''
    try {
        $si = [Console]::OpenStandardInput()
        $ms = New-Object System.IO.MemoryStream
        $si.CopyTo($ms)
        $raw = [System.Text.Encoding]::UTF8.GetString($ms.ToArray())
        $ms.Dispose(); $si.Dispose()
    } catch { $raw = '' }
    # UTF8.GetString 会把 BOM 解成 U+FEFF，这里用码位剥掉（不写字面量，
    # 免得被编码规范化工具弄没）。
    $raw = $raw.TrimStart([char]0xFEFF)

    if ($Agent -eq 'Codex') {
        # Codex 的 PermissionRequest 与 Claude Notification 字段不同。
        # 仅接受已支持的事件；绝不输出 allow/deny，也不更改审批决定。
        $pl = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($pl.hook_event_name -ne 'PermissionRequest') { exit 0 }
        $Kind = 'needs_input'
        $message = 'Codex 正在等待权限批准。'
        if ($pl.tool_input.description -is [string] -and $pl.tool_input.description.Trim()) {
            $message = $pl.tool_input.description
        }
        if ($pl.tool_name -is [string] -and $pl.tool_name.Trim()) {
            $message = ('工具：{0}；{1}' -f $pl.tool_name, $message)
        }
        # 命令和 MCP 参数可能有密钥：只发送审批说明，不转发完整 tool_input。
        # 用其哈希区分同轮次中不同的请求，避免两个不同命令被误去重。
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            # PowerShell 5.1 的 ConvertTo-Json 对 null 不输出文本。
            $inputJson = if ($null -eq $pl.tool_input) { 'null' }
                         else { ConvertTo-Json -InputObject $pl.tool_input -Depth 50 -Compress }
            $requestKey = [BitConverter]::ToString($sha.ComputeHash(
                [System.Text.Encoding]::UTF8.GetBytes($inputJson))) -replace '-', ''
        } finally { $sha.Dispose() }
        $raw = [ordered]@{
            agent = 'Codex'; hook_event_name = 'PermissionRequest'
            session_id = $pl.session_id; turn_id = $pl.turn_id; cwd = $pl.cwd
            message = $message; request_key = $requestKey
        } | ConvertTo-Json -Depth 5 -Compress
    }

    if ($Kind -notin @('needs_input', 'error')) { exit 0 }
    $root = Split-Path -Parent $PSScriptRoot
    $stateDir = Join-Path $root 'state'
    if (-not (Test-Path -LiteralPath $stateDir)) {
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    }
    $utf8 = New-Object System.Text.UTF8Encoding($false)

    $pf = Join-Path $stateDir ("hook_{0}_{1}.json" -f $Kind, ([guid]::NewGuid().ToString('N').Substring(0, 8)))
    [System.IO.File]::WriteAllText($pf, $raw, $utf8)

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
    $psi.Arguments = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Kind {1}' -f `
        (Join-Path $root 'scripts\wx_notify.ps1'), $Kind)
    if ($pf) { $psi.Arguments += (' -PayloadFile "{0}"' -f $pf) }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    [System.Diagnostics.Process]::Start($psi) | Out-Null   # 不 WaitForExit：立刻返回
} catch {
    # 后台进程没启动时也清理临时 payload，失败不影响主会话。
    if ($pf -and (Test-Path -LiteralPath $pf)) {
        Remove-Item -LiteralPath $pf -Force -ErrorAction SilentlyContinue
    }
}

exit 0
