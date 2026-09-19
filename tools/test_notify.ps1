<#
.SYNOPSIS
    wx_notify.ps1 的干跑自测：造假 payload 走各个分支，核对日志与队列。
    不需要微信在线；桌面锁屏时走的是队列分支，同样算通过。
#>
param([switch]$KeepQueue)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Root = Split-Path -Parent $PSScriptRoot
$StateDir = Join-Path $Root 'state'
$Notify = Join-Path $Root 'scripts\wx_notify.ps1'
$Switch = Join-Path $Root 'scripts\wx_switch.ps1'
$LogPath = Join-Path $StateDir 'notify.log'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$SID = 'selftest_session'

# ---- exit 会不会触发 finally？后面的 payload 清理全靠它 ----
$probe = Join-Path $StateDir '_exit_finally_probe.txt'
Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
$inner = Join-Path $StateDir '_exit_probe.ps1'
[System.IO.File]::WriteAllText($inner,
    "try { exit 3 } finally { [System.IO.File]::WriteAllText('$($probe -replace '\\','\\')','ran') }`r`n", $utf8)
& (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
    -NoProfile -ExecutionPolicy Bypass -File $inner | Out-Null
$finallyRuns = Test-Path -LiteralPath $probe
Write-Output ("[0] exit 触发 finally : {0}" -f $(if ($finallyRuns) { 'YES' } else { 'NO —— payload 会漏！' }))
Remove-Item -LiteralPath $inner, $probe -Force -ErrorAction SilentlyContinue

function New-Payload([string]$ev, [hashtable]$extra) {
    $o = @{ session_id = $SID; hook_event_name = $ev; cwd = $Root }
    foreach ($k in $extra.Keys) { $o[$k] = $extra[$k] }
    $p = Join-Path $StateDir ("_selftest_{0}.json" -f ([guid]::NewGuid().ToString('N').Substring(0, 6)))
    [System.IO.File]::WriteAllText($p, ($o | ConvertTo-Json -Depth 4), $utf8)
    return $p
}

# 在场判断会让自测变得不确定：你跑自测就得敲键盘，于是[4][5][6]必然被它拦下。
# 所以除了专测它的那一步，其余用例统一把阈值置 0 关掉这道闸。
# config.json 在 finally 里整体还原，这里直接改没关系。
function Set-Presence([int]$sec) {
    $c = [System.IO.File]::ReadAllText($cfgPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    $c.presence_idle_min_seconds = $sec
    [System.IO.File]::WriteAllText($cfgPath, ($c | ConvertTo-Json -Depth 5), $utf8)
}

# 自测要覆盖所有代码路径，不受用户日常配置影响：两个触发器统一强制打开，
# 专测闸门的那一步自己再关回去。
function Set-Triggers([bool]$needsIn, [bool]$err) {
    $c = [System.IO.File]::ReadAllText($cfgPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    $c.triggers.needs_input = $needsIn
    $c.triggers.error = $err
    [System.IO.File]::WriteAllText($cfgPath, ($c | ConvertTo-Json -Depth 5), $utf8)
}

# 同理：日常配置里 queue_when_unreachable 是 false（锁屏和抢不到前台都直接丢，
# 用户的决定），但 [4][6] 期望「已排队 或 发送成功」—— 真碰上这两种情况会打印"丢弃"而 FAIL。
# 这里强制打开，让排队那条路仍然可测；专测丢弃的那一步自己再关掉。
function Set-QueueUnreach([bool]$v) {
    $c = [System.IO.File]::ReadAllText($cfgPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    $c | Add-Member -NotePropertyName queue_when_unreachable -NotePropertyValue $v -Force
    [System.IO.File]::WriteAllText($cfgPath, ($c | ConvertTo-Json -Depth 5), $utf8)
}

function Tail-Log { if (Test-Path -LiteralPath $LogPath) { (Get-Content -LiteralPath $LogPath -Tail 1) } else { '' } }

function Run-Case([string]$name, [string]$kind, [string]$payload, [string]$expect) {
    $before = @(Get-ChildItem -LiteralPath $StateDir -Filter '_selftest_*.json' -EA SilentlyContinue).Count
    & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
        -NoProfile -ExecutionPolicy Bypass -File $Notify -Kind $kind -PayloadFile $payload | Out-Null
    $line = Tail-Log
    $leak = Test-Path -LiteralPath $payload
    $ok = ($line -match $expect)
    # 必须用 Write-Host：函数里任何未被捕获的 Write-Output 都会混进返回值，
    # 结果 $results 里存的是数组而不是布尔，判定永远为真。
    Write-Host ("{0,-34} {1}  {2}" -f $name, $(if ($ok) { 'PASS' } else { 'FAIL' }), ($line -replace '^\[.*?\]\s*', ''))
    if ($leak) { Write-Host ("    !! payload 未清理: {0}" -f (Split-Path -Leaf $payload)) }
    return ($ok -and -not $leak)
}

# ---- 记住原状态，最后恢复 ----
$cfgPath = Join-Path $Root 'config.json'
$cfgBak = [System.IO.File]::ReadAllText($cfgPath, [System.Text.Encoding]::UTF8)
$qdir = Join-Path $StateDir 'queue'
$qBefore = @(Get-ChildItem -LiteralPath $qdir -Filter '*.json' -EA SilentlyContinue |
    Select-Object -ExpandProperty Name)

# 去重记录必须清空再跑：用例 [4][6] 的正文是固定的，而去重键（刻意）不含时间，
# 所以 90s 内连跑两次自测，第二次会被上一次的记录正确地去重掉 —— 那是去重在干活，
# 不是 bug，但会让这两步误报 FAIL。跑完原样放回，不动用户真实的去重记录。
$sentPath = Join-Path $StateDir 'last_sent.json'
$sentBak = if (Test-Path -LiteralPath $sentPath) {
    [System.IO.File]::ReadAllText($sentPath, [System.Text.Encoding]::UTF8)
} else { $null }
Remove-Item -LiteralPath $sentPath -Force -ErrorAction SilentlyContinue

$results = New-Object System.Collections.ArrayList
try {
    # ================= 总开关关闭 =================
    & $Switch -Off | Out-Null
    $null = $results.Add((Run-Case '[1] 开关关闭 -> 不发' 'needs_input' `
        (New-Payload 'Notification' @{ message = 'needs permission' }) '总开关关闭'))

    # ================= 总开关开启 =================
    & $Switch -On | Out-Null
    Set-Presence 0          # 先关掉在场判断，让下面几步只受各自的条件影响
    Set-Triggers $true $true
    Set-QueueUnreach $true

    # 锁屏时落队列，未锁屏时直接发成功，两者都算这一步通过。
    # 这一步会真发一条微信（约 40 秒，夺前台）—— 下面 [3] 的去重要靠它先记下时间戳。
    $null = $results.Add((Run-Case '[2] 需要处理 -> 发/排队' 'needs_input' `
        (New-Payload 'Notification' @{ message = '两个用例全部通过。' }) '(已排队|发送成功)'))

    # 同内容再来一次应命中去重。
    # 窗口临时放大到 1 小时：去重时间戳是在"决定要发"时记的，而上一步真发一条要
    # 40 秒以上（碰上补发重试会到 100 秒），拿默认的 90 秒跑，这一步开始时窗口
    # 早过了，于是去重"正确地"不命中，却被记成 FAIL。
    $cTmp = [System.IO.File]::ReadAllText($cfgPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    $cTmp.dedupe_seconds = 3600
    [System.IO.File]::WriteAllText($cfgPath, ($cTmp | ConvertTo-Json -Depth 5), $utf8)
    $null = $results.Add((Run-Case '[3] 同内容重复 -> 去重' 'needs_input' `
        (New-Payload 'Notification' @{ message = '两个用例全部通过。' }) '去重跳过'))

    # error 的去重键含 kind，不会撞上上面 needs_input 的记录，放大的窗口对它无害。
    $null = $results.Add((Run-Case '[4] 出错中断' 'error' `
        (New-Payload 'StopFailure' @{ error_message = 'rate_limit: too many requests' }) `
        '(已排队|发送成功)'))
    $cTmp.dedupe_seconds = 90
    [System.IO.File]::WriteAllText($cfgPath, ($cTmp | ConvertTo-Json -Depth 5), $utf8)

    # 在场判断：阈值给到 24 小时，那么"刚敲过键盘"必然成立（你正在跑自测），
    # 这一步就该被拦住。正文用随机串，避免撞上前面的去重记录。
    Set-Presence 86400
    $null = $results.Add((Run-Case '[5] 人在机器前 -> 不打扰' 'needs_input' `
        (New-Payload 'Notification' @{ message = ('在场判断自测 ' + [guid]::NewGuid().ToString('N').Substring(0, 8)) }) `
        '你在机器前'))
    Set-Presence 0

    # dispatch.ps1 全链路（stdin 带 BOM 的 UTF-8 → spawn → 正文落队列）。
    # 这一步替掉了原来专测"回合起点"的三步：那套机制随 long_task_done 一起删了。
    # 真正要守住的是 dispatch 自己读字节再按 UTF-8 解码这一段 —— 本机控制台
    # InputEncoding 是 CP936，谁哪天顺手改成 [Console]::In.ReadToEnd()，开头的
    # "EF BB BF 7B" 就会变成「锘縶」，中文摘要整段乱码，而且没有任何报错。
    #
    # 必须用 cmd 的 type 灌进去，不能用 PowerShell 管道传字符串：PowerShell 会
    # 按自己的编码重新编一遍，BOM 那三个字节根本到不了被测代码，等于没测。
    #
    # sender_script 指向一个 exit 2 的桩：既不碰微信（不夺前台、不花 40 秒），
    # 又能让正文确定地落进队列，从队列文件里核对中文有没有活着到终点。
    $stub = Join-Path $StateDir '_selftest_stub_sender.ps1'
    [System.IO.File]::WriteAllText($stub,
        "param([string]`$Target='',[string]`$MessageFile='')`r`nexit 2`r`n", $utf8)
    $cStub = [System.IO.File]::ReadAllText($cfgPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    $cStub | Add-Member -NotePropertyName sender_script -NotePropertyValue $stub -Force
    [System.IO.File]::WriteAllText($cfgPath, ($cStub | ConvertTo-Json -Depth 5), $utf8)

    $marker = '中文摘要 ' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    $bomJson = Join-Path $StateDir '_selftest_bom.json'
    [System.IO.File]::WriteAllText($bomJson,
        (@{ session_id = $SID; hook_event_name = 'Notification'; cwd = $Root; message = $marker } |
            ConvertTo-Json -Compress),
        (New-Object System.Text.UTF8Encoding($true)))     # $true = 带 BOM，就是 hook 真实的样子
    $qSnap = @(Get-ChildItem -LiteralPath $qdir -Filter '*.json' -EA SilentlyContinue |
        Select-Object -ExpandProperty Name)
    & "$env:ComSpec" /c ('type "{0}" | "{1}" -NoProfile -ExecutionPolicy Bypass -File "{2}" -Kind needs_input' -f `
            $bomJson, (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'), `
        (Join-Path $Root 'hooks\dispatch.ps1')) | Out-Null

    # dispatch 故意不等子进程（hook 必须秒退），所以这里轮询等它落盘。
    $newQ = $null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        $newQ = @(Get-ChildItem -LiteralPath $qdir -Filter '*.json' -EA SilentlyContinue |
            Where-Object { $qSnap -notcontains $_.Name }) | Select-Object -First 1
        if ($newQ) { break }
    }
    $dispOk = $false
    $dispWhy = '队列里没出现新文件'
    if ($newQ) {
        $qBody = ''
        try {
            $qBody = ([System.IO.File]::ReadAllText($newQ.FullName, [System.Text.Encoding]::UTF8) |
                ConvertFrom-Json).body
        } catch {}
        $dispOk = ($qBody -like ("*{0}*" -f $marker))
        $dispWhy = if ($dispOk) { '中文原样到达队列' } else { ("正文里找不到标记：{0}" -f (($qBody -replace "`n", '\n'))) }
    }
    Write-Output ("{0,-34} {1}  {2}" -f '[6] dispatch 全链路(BOM/中文)', `
        $(if ($dispOk) { 'PASS' } else { 'FAIL' }), $dispWhy)
    $null = $results.Add($dispOk)
    Remove-Item -LiteralPath $bomJson, $stub -Force -EA SilentlyContinue
    [System.IO.File]::WriteAllText($cfgPath, $cfgBak, $utf8)
    & $Switch -On | Out-Null
    Set-Presence 0
    Set-Triggers $true $true
    Set-QueueUnreach $true


    # 队列过期：造一条超龄的，补发时应该被丢掉而不是发出去。
    # 这一步锁屏下也能验 —— 过期判断在调用发送器之前。
    $qdir = Join-Path $StateDir 'queue'
    if (-not (Test-Path -LiteralPath $qdir)) { $null = New-Item -ItemType Directory -Path $qdir -Force }
    $expName = '19700101_000000_needs_input_selftest.json'
    $expPath = Join-Path $qdir $expName
    $old = (Get-Date).AddHours(-99).ToString('o')
    [System.IO.File]::WriteAllText($expPath, (@{
                created_at = $old; target = '文件传输助手'
                body = '[自测] 这条应当因过期被丢弃'; attempts = 0; kind = 'needs_input'
            } | ConvertTo-Json -Compress), $utf8)
    & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
        -NoProfile -ExecutionPolicy Bypass -File $Notify -FlushOnly | Out-Null
    $gone = -not (Test-Path -LiteralPath $expPath)
    Write-Output ("{0,-34} {1}" -f '[7] 队列超龄 -> 丢弃', $(if ($gone) { 'PASS' } else { 'FAIL' }))
    $null = $results.Add($gone)
    Remove-Item -LiteralPath $expPath -Force -EA SilentlyContinue

    # 发送脚本缺失：必须跳过，不能排队。
    # 排了队的话每次触发都会重试、每次都失败，攒到 5 次上限被丢掉，而真正的原因
    # （路径配错了）被埋在一堆"发送失败"里。所以这一步同时验两件事：日志说对了原因，队列没长。
    $qBeforeMiss = @(Get-ChildItem -LiteralPath (Join-Path $StateDir 'queue') -Filter '*.json' -EA SilentlyContinue).Count
    $cMiss = [System.IO.File]::ReadAllText($cfgPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    $cMiss | Add-Member -NotePropertyName sender_script `
        -NotePropertyValue 'C:\__no_such_dir__\wx_send.ps1' -Force
    [System.IO.File]::WriteAllText($cfgPath, ($cMiss | ConvertTo-Json -Depth 5), $utf8)
    $missOk = Run-Case '[8] 发送脚本缺失 -> 跳过' 'needs_input' `
        (New-Payload 'Notification' @{ message = ('缺发送器自测 ' + [guid]::NewGuid().ToString('N').Substring(0, 8)) }) `
        '找不到发送脚本'
    $qAfterMiss = @(Get-ChildItem -LiteralPath (Join-Path $StateDir 'queue') -Filter '*.json' -EA SilentlyContinue).Count
    if ($qAfterMiss -ne $qBeforeMiss) {
        Write-Output ("    !! 队列长了 {0} 条，本该跳过而不是排队" -f ($qAfterMiss - $qBeforeMiss))
        $missOk = $false
    }
    $null = $results.Add($missOk)
    # 还原配置，后面的用例要能正常找到发送器
    [System.IO.File]::WriteAllText($cfgPath, $cfgBak, $utf8)
    & $Switch -On | Out-Null
    Set-Presence 0
    Set-Triggers $true $true
    Set-QueueUnreach $true

    # -WhichSender 是 wx_switch.ps1 和 install.py 共用的那个"发送脚本在哪"的唯一来源，
    # 它们靠它才不用各自抄一份候选路径。所以它本身得有人看着：坏了那两处都会静默失真。
    $wsOut = & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
        -NoProfile -ExecutionPolicy Bypass -File $Notify -WhichSender 2>$null
    $wsPath = if ($wsOut) { ([string](@($wsOut)[0])).Trim() } else { '' }
    $wsOk = $wsPath -and (Test-Path -LiteralPath $wsPath -PathType Leaf)
    Write-Output ("{0,-34} {1}  {2}" -f '[9] -WhichSender 指向真文件', `
        $(if ($wsOk) { 'PASS' } else { 'FAIL' }), $(if ($wsPath) { $wsPath } else { '(空)' }))
    $null = $results.Add($wsOk)

    # 触发器闸门：单独关掉一个触发器时它得真拦住。用 error 那条来验 ——
    # needs_input 后面没有别的用例要用，但 error 关掉更省事：不会真发微信。
    Set-Triggers $true $false
    $null = $results.Add((Run-Case '[10] 触发器关闭 -> 不发' 'error' `
        (New-Payload 'StopFailure' @{ error_message = ('闸门自测 ' + [guid]::NewGuid().ToString('N').Substring(0, 8)) }) `
        '该触发器已关闭'))
} finally {
    [System.IO.File]::WriteAllText($cfgPath, $cfgBak, $utf8)
    if ($null -ne $sentBak) {
        [System.IO.File]::WriteAllText($sentPath, $sentBak, $utf8)
    } else {
        Remove-Item -LiteralPath $sentPath -Force -ErrorAction SilentlyContinue
    }
    Get-ChildItem -LiteralPath $StateDir -Filter '_selftest_*' -EA SilentlyContinue |
        Remove-Item -Force -EA SilentlyContinue
    if (-not $KeepQueue) {
        # 只删本次自测新产生的队列文件，用户真实积压的不动
        Get-ChildItem -LiteralPath $qdir -Filter '*.json' -EA SilentlyContinue |
            Where-Object { $qBefore -notcontains $_.Name } |
            Remove-Item -Force -EA SilentlyContinue
    }
}

$pass = @($results | Where-Object { $_ }).Count
Write-Output ''
Write-Output ("结果: {0}/{1} 通过（config.json 与队列已还原）" -f $pass, $results.Count)
exit $(if ($pass -eq $results.Count) { 0 } else { 1 })

