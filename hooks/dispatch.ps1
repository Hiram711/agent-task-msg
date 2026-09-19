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
    [string]$Kind = ''
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

    $root = Split-Path -Parent $PSScriptRoot
    $stateDir = Join-Path $root 'state'
    if (-not (Test-Path -LiteralPath $stateDir)) {
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    }
    $utf8 = New-Object System.Text.UTF8Encoding($false)

    if ($Kind -eq '') { exit 0 }

    $pf = Join-Path $stateDir ("hook_{0}_{1}.json" -f $Kind, ([guid]::NewGuid().ToString('N').Substring(0, 8)))
    try { [System.IO.File]::WriteAllText($pf, $raw, $utf8) } catch { $pf = '' }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
    $psi.Arguments = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Kind {1}' -f `
        (Join-Path $root 'scripts\wx_notify.ps1'), $Kind)
    if ($pf) { $psi.Arguments += (' -PayloadFile "{0}"' -f $pf) }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    [System.Diagnostics.Process]::Start($psi) | Out-Null   # 不 WaitForExit：立刻返回
} catch {}

exit 0
