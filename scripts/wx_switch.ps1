<#
.SYNOPSIS
    AI-Sender 推送总开关。默认关闭，只有显式开启后才会推任何消息。

.EXAMPLE
    wx_switch.ps1 -Status
    wx_switch.ps1 -On
    wx_switch.ps1 -Off
    wx_switch.ps1 -Target "文件传输助手"
#>
param(
    [switch]$On,
    [switch]$Off,
    [switch]$Status,
    [string]$Target = ''
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Root = Split-Path -Parent $PSScriptRoot
$CfgPath = Join-Path $Root 'config.json'
if (-not (Test-Path -LiteralPath $CfgPath)) { Write-Output "!! 配置不存在: $CfgPath"; exit 1 }

$raw = [System.IO.File]::ReadAllText($CfgPath, [System.Text.Encoding]::UTF8) -replace '^﻿', ''
$cfg = $raw | ConvertFrom-Json

if ($On -and $Off) { Write-Output '!! -On 和 -Off 不能同时用'; exit 1 }

$changed = $false
if ($On) { $cfg.enabled = $true; $changed = $true }
if ($Off) { $cfg.enabled = $false; $changed = $true }
if ($Target) { $cfg.target = $Target; $changed = $true }

if ($changed) {
    $json = $cfg | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($CfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

$qn = 0
$qdir = Join-Path $Root 'state\queue'
if (Test-Path -LiteralPath $qdir) {
    $qn = @(Get-ChildItem -LiteralPath $qdir -Filter '*.json' -ErrorAction SilentlyContinue).Count
}

Write-Output ("推送总开关 : {0}" -f $(if ($cfg.enabled) { '已开启' } else { '已关闭（不会发任何消息）' }))
Write-Output ("目标会话   : {0}" -f $cfg.target)
$question = if ($null -eq $cfg.triggers.question) { $true } else { $cfg.triggers.question }
$needsInput = if ($null -eq $cfg.triggers.needs_input) { $true } else { $cfg.triggers.needs_input }
$permission = if ($null -eq $cfg.triggers.permission_request) { $needsInput } else { $cfg.triggers.permission_request }
Write-Output ("触发器     : 需要处理={0} 出错中断={1} 等待回答={2} 人工权限预提醒={3}" -f `
        $needsInput, $cfg.triggers.error, $question, $permission)

# 发送脚本在不在也要报：它是独立技能 wechat-send，缺了整套就发不出去，
# 而那种失败只会出现在 state/notify.log 里，用户看 -Status 是看不见的。
# 不在这儿重抄一份候选路径 —— 去问 wx_notify.ps1，它才是运行时真正用的那份逻辑。
# 抄一份的话两边会静默走样：这里报"找到了"，真发的时候却找不到。
$senderPath = ''
try {
    $senderPath = (& (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
            -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'wx_notify.ps1') `
            -WhichSender 2>$null | Select-Object -First 1)
    if ($null -ne $senderPath) { $senderPath = ([string]$senderPath).Trim() }
} catch { $senderPath = '' }
if ($senderPath) {
    Write-Output ("发送脚本   : {0}" -f $senderPath)
} else {
    Write-Output '发送脚本   : !! 找不到'
    $sc = if ($null -ne $cfg.sender_script) { [string]$cfg.sender_script } else { '' }
    if ($sc) { Write-Output ("             config.json 的 sender_script 指向 {0}，那儿没有文件" -f $sc) }
    Write-Output '             ↑ 发送技能 wechat-send 缺失，开着开关也发不出去。'
    Write-Output '               把 wechat-send-skill 放到本项目同级目录，或把'
    Write-Output '               wx_send.ps1 的绝对路径填进 config.json 的 sender_script。'
}
$age = if ($null -ne $cfg.queue_max_age_hours) { [int]$cfg.queue_max_age_hours } else { 24 }
Write-Output ("待补发队列 : {0} 条（超过 {1} 小时的丢弃）" -f $qn, $age)
# 关着开关时连补发也不会走，队列会一直压着，这点得说出来，否则用户
# 看到"待补发 N 条"会以为它们迟早自己出去。
if ($qn -gt 0 -and -not $cfg.enabled) {
    Write-Output '             ↑ 开关关闭时不补发，这些会一直压着（超时后丢弃）'
}
if ($changed) { Write-Output '(配置已写入)' }
exit 0
