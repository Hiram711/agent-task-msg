<#
.SYNOPSIS
    AI-Sender 通知核心：按配置决定是否推送，组装带摘要的正文，调用 wx_send.ps1 发出。

.DESCRIPTION
    由 hooks/dispatch.ps1 在后台进程里调用，不直接挂到 hook 上（hook 必须秒退）。

    职责：
      1. 读 config.json，总开关 enabled=false 时什么都不做（默认就是 false）。
      2. 按 Event 判断该触发器是否开启。
      3. 在场判断：你正在用键鼠时不发（发送要夺前台约 40 秒，你人在跟前就纯属打断）。
      4. 去重：同一事件 + 同一正文在 dedupe_seconds 内只发一次。
      5. 桌面够不着（wx_send 退出码 8 锁屏 / 3 抢不到前台）时按 queue_when_unreachable
         决定排队补发还是直接丢，默认丢。
      6. 发送前先冲队列，保证消息顺序。

.PARAMETER Kind
    needs_input | error | question。question 由 Codex 主动提问入口调用。

.PARAMETER PayloadFile
    hook 原始 stdin JSON 的落盘路径。

.PARAMETER FlushOnly
    只补发队列，不产生新消息。手动执行用（"现在就把积压的发出来"），
    因此它故意绕过在场判断 —— 是你自己叫的，不该再替你判断该不该打断。

.PARAMETER WhichSender
    只打印解析到的发送脚本路径然后退出，不做任何别的事。
    存在的唯一理由是让 wx_switch.ps1 和 install.py 别各自再抄一份候选路径列表：
    那种三处硬编码的耦合会静默走样（报告里说找到了，实际运行时找不到）。
    找到打印路径、退出 0；找不到打印空行、退出 9。

.OUTPUTS
    退出码恒为 0（除 -WhichSender）：通知失败绝不能影响主会话。
    所有细节写入 state/notify.log。
#>
param(
    # 不要叫 $Event：PowerShell 里 $Event 是事件相关的自动变量，会打架。
    [ValidateSet('needs_input', 'question', 'error')]
    [string]$Kind = '',
    [string]$PayloadFile = '',
    [switch]$FlushOnly,
    [switch]$WhichSender
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Root = Split-Path -Parent $PSScriptRoot
$CfgPath = Join-Path $Root 'config.json'
$StateDir = Join-Path $Root 'state'
$QueueDir = Join-Path $StateDir 'queue'
$LogPath = Join-Path $StateDir 'notify.log'
$SentPath = Join-Path $StateDir 'last_sent.json'
# 真正操作微信客户端的那一层已经拆成独立技能 wechat-send，本脚本只是它的调用方。
# 按顺序找，第一个存在的就用：
#   1. config.json 的 sender_script（用户把发送技能放在别处时填这个）
#   2. 同级目录的 wechat-send-skill（默认布局：两个技能并排放）
#   3. 同级目录的 wechat-send（按技能名安装时的布局）
#   4. 本项目 scripts\wx_send.ps1（拆分之前的老布局，留着兼容）
# 一个都找不到不能当成"锁屏"去排队 —— 那会让队列里的消息永远重试永远失败，
# 还把真正的原因埋掉。Resolve-Sender 返回 $null，调用方据此报配置错误。
$SenderCandidates = @(
    (Join-Path (Split-Path -Parent $Root) 'wechat-send-skill\scripts\wx_send.ps1'),
    (Join-Path (Split-Path -Parent $Root) 'wechat-send\scripts\wx_send.ps1'),
    (Join-Path $PSScriptRoot 'wx_send.ps1')
)
function Resolve-Sender([string]$fromCfg) {
    if ($fromCfg) {
        # 配置里写了就只认它：写了却找不到是配置错误，不该悄悄退回默认路径，
        # 否则用户以为在用自己指定的那份，实际用的是别的。
        if (Test-Path -LiteralPath $fromCfg -PathType Leaf) { return $fromCfg }
        return $null
    }
    foreach ($p in $SenderCandidates) {
        if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    }
    return $null
}

function Ensure-Dirs {
    foreach ($d in @($StateDir, $QueueDir)) {
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }
}

function Write-Utf8([string]$path, [string]$text) {
    # 不写 BOM：队列文件与临时正文文件都由本套脚本自己读，统一按 UTF-8 无 BOM 处理。
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

function Log([string]$m) {
    try {
        Ensure-Dirs
        $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $m
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    } catch {}
}

function Rotate-Log([int]$maxKb) {
    try {
        if ($maxKb -le 0) { return }
        if (-not (Test-Path -LiteralPath $LogPath)) { return }
        if ((Get-Item -LiteralPath $LogPath).Length -le ($maxKb * 1KB)) { return }
        Move-Item -LiteralPath $LogPath -Destination "$LogPath.old" -Force
    } catch {}
}

function Read-Json([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try {
        $raw = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
        if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
        # 用码位剥 BOM，不要在源码里写字面量 U+FEFF：编辑器/编码规范化工具会把它弄没。
        return ($raw.TrimStart([char]0xFEFF) | ConvertFrom-Json)
    } catch { return $null }
}

function Get-Cfg {
    $c = Read-Json $CfgPath
    if ($null -eq $c) { return $null }
    # 缺字段时给保守默认值；enabled 缺失按 false 处理，绝不默认开启。
    function Def($v, $d) { if ($null -eq $v) { return $d } return $v }
    return [pscustomobject]@{
        Enabled     = [bool](Def $c.enabled $false)
        Target      = [string](Def $c.target '文件传输助手')
        SenderPath  = [string](Def $c.sender_script '')
        TrNeedsIn   = [bool](Def $c.triggers.needs_input $true)
        TrQuestion  = [bool](Def $c.triggers.question $true)
        TrError     = [bool](Def $c.triggers.error $true)
        PresenceSec = [int](Def $c.presence_idle_min_seconds 120)
        SumMax      = [int](Def $c.summary_max_chars 220)
        DedupeSec   = [int](Def $c.dedupe_seconds 90)
        # 桌面这会儿够不着（锁屏 exit 8 / 抢不到前台 exit 3）时排不排队。
        # 默认 false = 丢。改名前叫 queue_when_locked，只管锁屏那一条；
        # 现在两条都管，名字得跟上，旧名还认（下面 Def 的第二个参数就是旧值）。
        QueueUnreach = [bool](Def $c.queue_when_unreachable (Def $c.queue_when_locked $false))
        QueueMaxAgeH = [int](Def $c.queue_max_age_hours 24)
        LogMaxKb    = [int](Def $c.log_max_kb 512)
    }
}

# ---------------------------------------------------------------- 在场判断
# 距离上一次键鼠输入过了多少秒。GetLastInputInfo 是整个会话级的（任何进程收到的
# 键鼠都算），正好是我们要的语义：不关心你在用哪个程序，只关心你人在不在。
# 注意它只能回答"有没有碰键鼠"，没法区分"在读长输出但没动手"——这是这条判断的固有
# 局限，阈值给宽一点来兜。
# Add-Type 放在函数里懒加载：UserPromptSubmit 每回合都会进这个脚本然后秒退，
# 不该让它白白付一次编译开销。
# 别给它补 -UsingNamespace 'System.Runtime.InteropServices'：-MemberDefinition 已经
# 自动 using 了这个命名空间，再加一次是重复 using，而这里警告被当成错误，
# 直接编译失败 → 整条在场判断静默退化成 -1（永远判"不在场"，等于这道闸失效）。
$script:IdleTypeReady = $false
function Get-IdleSeconds {
    if (-not $script:IdleTypeReady) {
        try {
            Add-Type -Namespace AiSender -Name Idle -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)]
public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
[DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO p);
// GetTickCount64 不像 GetTickCount 那样 49.7 天回绕，省掉一处只在开机很久后才
// 发作的减法坑。dwTime 本身仍是 32 位的 tick，用 64 位的低 32 位去减。
[DllImport("kernel32.dll")] static extern ulong GetTickCount64();
public static double Seconds() {
    LASTINPUTINFO i = new LASTINPUTINFO();
    i.cbSize = (uint)Marshal.SizeOf(i);
    if (!GetLastInputInfo(ref i)) return -1.0;
    ulong now = GetTickCount64();
    ulong last = (now & 0xFFFFFFFF00000000UL) | (ulong)i.dwTime;
    if (last > now) last -= 0x100000000UL;   // 刚好跨过 32 位边界
    return (now - last) / 1000.0;
}
'@ -ErrorAction Stop
        } catch {
            Log ("!! 在场判断不可用（{0}），本次按'不在场'处理" -f $_.Exception.Message)
            return -1.0
        }
        $script:IdleTypeReady = $true
    }
    try { return [AiSender.Idle]::Seconds() } catch { return -1.0 }
}

# ---------------------------------------------------------------- 正文组装
function Clip-Text([string]$s, [int]$max) {
    if ([string]::IsNullOrEmpty($s)) { return '' }
    # 折叠所有空白（含换行）再截断：摘要要短，原文的排版没意义。
    $t = ($s -replace '\s+', ' ').Trim()
    if ($t.Length -le $max) { return $t }
    return $t.Substring(0, $max) + '…'
}

function Build-Body($cfg, [string]$ev, $pl) {
    $agentName = if ($pl -and $pl.agent -eq 'Codex') { 'Codex' } else { 'Claude Code' }
    $head = switch ($ev) {
        'needs_input'    { "[$agentName] 需要你处理" }
        'question'       { "[$agentName] 等待你回答" }
        'error'          { "[$agentName] 出错中断" }
    }
    $proj = ''
    if ($pl -and $pl.cwd) { $proj = Split-Path -Leaf ([string]$pl.cwd) }
    $lines = New-Object System.Collections.ArrayList
    $null = $lines.Add($head)
    $null = $lines.Add(("时间：{0:MM-dd HH:mm:ss}" -f (Get-Date)))
    if ($proj) { $null = $lines.Add("项目：$proj") }

    # 摘要来源按事件取：Notification 给 message；StopFailure 的错误字段名各版本
    # 未必一致，按优先级逐个兜。
    $sum = ''
    if ($pl) {
        $cands = switch ($ev) {
            'needs_input'    { @($pl.message, $pl.notification, $pl.last_assistant_message) }
            'question'       { @($pl.message) }
            'error'          { @($pl.error_message, $pl.error, $pl.message, $pl.last_assistant_message) }
        }
        foreach ($c in $cands) {
            if ($c -and -not [string]::IsNullOrWhiteSpace([string]$c)) { $sum = [string]$c; break }
        }
    }
    $sumClip = ''
    if ($sum) {
        $sumClip = Clip-Text $sum $cfg.SumMax
        $null = $lines.Add('—')
        $null = $lines.Add($sumClip)
    }
    # Key 供去重用，必须剔掉时间那一行：它每次都不一样，拿整条正文算哈希等于永不去重。
    $scope = ''
    if ($agentName -eq 'Codex') {
        $scope = '|{0}|{1}|{2}' -f $pl.session_id, $pl.turn_id, $pl.request_key
    }
    return [pscustomobject]@{
        Body = ($lines -join "`n")
        Key  = ($head + '|' + $proj + '|' + $sumClip + $scope)
    }
}

# ---------------------------------------------------------------- 调用发送器
function Invoke-Sender([string]$target, [string]$body, [string]$senderPs1) {
    # 正文走临时文件，不走命令行参数：正文含换行、引号、中文，拼进命令行迟早出事。
    $tmp = Join-Path $StateDir ("body_{0}.txt" -f ([guid]::NewGuid().ToString('N').Substring(0, 8)))
    Write-Utf8 $tmp $body
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
        $psi.Arguments = ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -Target "{1}" -MessageFile "{2}"' -f `
                $senderPs1, $target, $tmp)
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
        $p = [System.Diagnostics.Process]::Start($psi)
        $so = $p.StandardOutput.ReadToEnd()
        $se = $p.StandardError.ReadToEnd()
        $p.WaitForExit()
        foreach ($l in ($so -split "`r?`n")) { if ($l.Trim()) { Log ("  | " + $l.Trim()) } }
        if ($se.Trim()) { Log ("  !ERR " + (Clip-Text $se 300)) }
        return $p.ExitCode
    } finally {
        try { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } catch {}
    }
}

# ---------------------------------------------------------------- 队列
function Enqueue([string]$target, [string]$body, [string]$kind) {
    Ensure-Dirs
    $name = ("{0:yyyyMMdd_HHmmss}_{1}_{2}.json" -f (Get-Date), $kind,
        ([guid]::NewGuid().ToString('N').Substring(0, 6)))
    $obj = [pscustomobject]@{
        created_at = (Get-Date).ToString('o'); target = $target; kind = $kind
        body = $body; attempts = 0
    }
    Write-Utf8 (Join-Path $QueueDir $name) ($obj | ConvertTo-Json -Depth 5)
    # 上限 30 条，超了丢最旧的：积压太久的提醒已无意义，也不该无限长胖。
    $all = @(Get-ChildItem -LiteralPath $QueueDir -Filter '*.json' -ErrorAction SilentlyContinue |
        Sort-Object Name)
    if ($all.Count -gt 30) {
        $all[0..($all.Count - 31)] | ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
        }
        Log ("队列超过 30 条，已丢弃最旧的 {0} 条" -f ($all.Count - 30))
    }
    Log ("已排队: {0}" -f $name)
}

function Flush-Queue([int]$maxAge = 24, [string]$senderPs1 = '') {
    # maxAge 和 senderPs1 都显式传进来，不靠动态作用域去摸外层的 $cfg：Def 只活在
    # Read-Config 内部，在这儿写 Def 会直接报「无法识别的命令」。
    $all = @(Get-ChildItem -LiteralPath $QueueDir -Filter '*.json' -ErrorAction SilentlyContinue |
        Sort-Object Name)
    if ($all.Count -eq 0) { return 0 }
    Log ("开始补发队列，共 {0} 条" -f $all.Count)
    $sent = 0
    foreach ($f in $all) {
        $q = Read-Json $f.FullName
        if ($null -eq $q) { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue; continue }
        # 过期就丢。队列可能压很久（关了开关就不补发，锁屏也不补发），
        # 而"需要你处理"这种通知过了时效就只是噪音：它指的那个回合早没了。
        $age = -1.0
        try { $age = ((Get-Date) - [datetime]$q.created_at).TotalHours } catch { $age = -1.0 }
        if ($maxAge -gt 0 -and $age -gt $maxAge) {
            Log ("{0} 已积压 {1:N1} 小时(上限 {2}h)，过期丢弃" -f $f.Name, $age, $maxAge)
            Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
            continue
        }
        $prefix = "[补发 {0:MM-dd HH:mm}]`n" -f ([datetime]$q.created_at)
        $rc = Invoke-Sender $q.target ($prefix + $q.body) $senderPs1
        if ($rc -eq 0) {
            Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
            $sent++
        } elseif ($rc -eq 8 -or $rc -eq 3) {
            # 桌面够不着，跟这条消息本身没关系：不算它的失败次数（白扣会让它提前被
            # 5 次上限丢掉），也不用再试后面几条，下次触发再说。
            $why = if ($rc -eq 8) { '桌面仍不可交互' } else { '仍抢不到前台' }
            Log ("{0}，队列保留，停止补发" -f $why)
            break
        } else {
            $n = [int]$q.attempts + 1
            if ($n -ge 5) {
                Log ("{0} 连续 5 次失败(最后退出码 {1})，放弃并删除" -f $f.Name, $rc)
                Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
            } else {
                $q.attempts = $n
                Write-Utf8 $f.FullName ($q | ConvertTo-Json -Depth 5)
                Log ("{0} 发送失败(退出码 {1})，第 {2} 次，保留重试" -f $f.Name, $rc, $n)
            }
            break
        }
    }
    return $sent
}

# 这儿以前有一组回合计时函数（Turn-File / Set-TurnStart / Get-Elapsed /
# Clear-TurnStart）：UserPromptSubmit 落开始时间，Stop 时算耗时，专门给
# long_task_done 筛短回合用。那个触发器和它的两个 hook 都摘了（2026-09-19），
# 计时也就没有读者了，一起删掉。别照着"耗时"字段的样子把它加回来。

# ---------------------------------------------------------------- 去重
function Body-Hash([string]$s) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $b = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes(($s -replace '\s', '')))
        return ([System.BitConverter]::ToString($b) -replace '-', '').Substring(0, 16)
    } finally { $sha.Dispose() }
}

function Test-Dupe([string]$kind, [string]$body, [int]$window) {
    if ($window -le 0) { return $false }
    $key = "$kind/$(Body-Hash $body)"
    $m = Read-Json $SentPath
    $now = Get-Date
    $map = @{}
    if ($m) {
        foreach ($p in $m.PSObject.Properties) {
            try {
                if (((Get-Date) - [datetime]::Parse($p.Value)).TotalSeconds -lt 3600) {
                    $map[$p.Name] = $p.Value
                }
            } catch {}
        }
    }
    $dupe = $false
    if ($map.ContainsKey($key)) {
        try { $dupe = ((($now) - [datetime]::Parse($map[$key])).TotalSeconds -lt $window) } catch {}
    }
    if (-not $dupe) {
        $map[$key] = $now.ToString('o')
        try { Write-Utf8 $SentPath ([pscustomobject]$map | ConvertTo-Json -Depth 3) } catch {}
    }
    return $dupe
}

# ================================================================ 主流程

# -WhichSender 在所有别的事情之前处理：它只回答"发送脚本在哪"，不该写日志、
# 不该建目录、不该碰队列。配置读不出来也照样答（退回默认候选），因为调用方正是
# 拿它来诊断"为什么发不出去"，那种时候配置本身就可能是坏的。
if ($WhichSender) {
    $c = $null
    try { $c = Get-Cfg } catch {}
    $sp = Resolve-Sender $(if ($c) { $c.SenderPath } else { '' })
    if ($sp) { Write-Output $sp; exit 0 }
    Write-Output ''
    exit 9
}

Ensure-Dirs
$pl = if ($PayloadFile) { Read-Json $PayloadFile } else { $null }
$sid = if ($pl -and $pl.session_id) { [string]$pl.session_id } else { 'nosession' }
$nMutex = $null
$owned = $false

# 所有提前退出都必须走到底部的 finally（PowerShell 的 exit 会触发 finally），
# 否则 payload 临时文件会漏。
try {
    $cfg = Get-Cfg
    if ($null -eq $cfg) { Log "!! 读不到配置 $CfgPath，本次跳过"; exit 0 }
    Rotate-Log $cfg.LogMaxKb

    # 没给 -Kind 又不是 -FlushOnly：没什么可做的。以前这儿是 UserPromptSubmit 的
    # 回合计时落盘，随 long_task_done 一起删了。
    if ($Kind -eq '' -and -not $FlushOnly) { exit 0 }

    # 这道闸故意放在 $FlushOnly 分支之前：开关关着时连补发也不做。
    # 有意如此 —— 开关的语义是"我现在不想被微信打扰"，那就包括积压的旧消息；
    # 代价是关着开关期间队列里的东西会一直躺到 queue_max_age_hours 过期。别"顺手修"。
    if (-not $cfg.Enabled) {
        Log ("总开关关闭，跳过 ({0})" -f $(if ($FlushOnly) { 'flush' } else { $Kind }))
        exit 0
    }

    # 定位发送器。放在这儿：总开关之后（关着就啥也不做），发送和补发两条路之前。
    # 找不到属于配置问题，不是"这次发不出去" —— 所以直接跳过，不排队：
    # 排了队也只会每次触发都重试、每次都失败，最后按 5 次上限丢掉，还把真正的
    # 原因埋在一堆"发送失败"里。日志里直接说清怎么修。
    $sender = Resolve-Sender $cfg.SenderPath
    if (-not $sender) {
        $hint = if ($cfg.SenderPath) { "config.json 的 sender_script 指向 '$($cfg.SenderPath)'，但那儿没有文件" }
        else { "默认位置都没找到：$($SenderCandidates -join '、')" }
        Log ("!! 找不到发送脚本，本次跳过。{0}。发送那一层是独立技能 wechat-send，放在别处就把 wx_send.ps1 的绝对路径填进 config.json 的 sender_script。" -f $hint)
        exit 0
    }

    # 串行闸：hook 是并发触发的，两个进程同时冲队列会把同一条发两遍。
    $nMutex = New-Object System.Threading.Mutex($false, 'Global\AiSenderNotifyMutex')
    try { $owned = $nMutex.WaitOne(120000) } catch { $owned = $false }

    if ($FlushOnly) {
        if (-not $owned) { Log '!! 等待通知锁超时，本次不补发'; exit 0 }
        $n = Flush-Queue $cfg.QueueMaxAgeH $sender
        if ($n -gt 0) { Log ("补发完成 {0} 条" -f $n) }
        exit 0
    }

    # 触发器开关
    switch ($Kind) {
        'needs_input' {
            if (-not $cfg.TrNeedsIn) { Log '需要处理：该触发器已关闭'; exit 0 }
        }
        'error' {
            if (-not $cfg.TrError) { Log '出错中断：该触发器已关闭'; exit 0 }
        }
        'question' {
            if (-not $cfg.TrQuestion) { Log '等待回答：该触发器已关闭'; exit 0 }
        }
    }

    # ---- 在场判断 ----
    # 放在 Flush-Queue 之前：人在跟前时连补发都不该抢前台。
    # 两个触发器一视同仁 —— 发一条要夺前台约 40 秒并把微信整个窗口拉到最上层，
    # 你正盯着屏幕的时候这毫无意义：该看见的你已经看见了。
    # 判断不出来（-1）就照发：宁可多打扰一次，不能漏掉真该提醒的。
    if ($cfg.PresenceSec -gt 0) {
        $idle = Get-IdleSeconds
        if ($idle -ge 0 -and $idle -lt $cfg.PresenceSec) {
            Log ("你在机器前（{0:N0}s 内有键鼠，阈值 {1}s），跳过 {2}" -f $idle, $cfg.PresenceSec, $Kind)
            exit 0
        }
        Log ("在场判断：空闲 {0}" -f $(if ($idle -lt 0) { '未知，按不在场处理' } else { ("{0:N0}s" -f $idle) }))
    }

    $msg  = Build-Body $cfg $Kind $pl
    $body = $msg.Body
    if (Test-Dupe $Kind $msg.Key $cfg.DedupeSec) {
        Log ("{0}s 内已发过同样内容，去重跳过" -f $cfg.DedupeSec)
        exit 0
    }

    if (-not $owned) {
        # 拿不到锁说明另一个通知正在发，直接排队，交给它或下一次补发。
        Log '!! 等待通知锁超时，转入队列'
        Enqueue $cfg.Target $body $Kind
        exit 0
    }

    Log ("触发 {0}" -f $Kind)
    $null = Flush-Queue $cfg.QueueMaxAgeH $sender   # 先清积压，保证顺序
    $rc = Invoke-Sender $cfg.Target $body $sender
    if ($rc -eq 0) {
        Log '发送成功'
    } elseif ($rc -eq 8 -or $rc -eq 3) {
        # 桌面这会儿够不着，不是脚本坏了：8 = 锁屏/息屏，3 = 抢不到前台（你正在用电脑）。
        # 用户的决定：两条都直接丢，不排队 —— "等我回来再触发已经不需要了"。
        # 这两种提醒指向的是某个具体时刻（现在要你批准 / 刚崩了），过了那个点就只是噪音：
        # 锁屏回来时该批的早卡在那儿摆着；抢不到前台时你人就在跟前，屏幕上都看得见。
        $why = if ($rc -eq 8) { '桌面不可交互（锁屏/息屏）' } else { '抢不到前台（你正在用电脑）' }
        if ($cfg.QueueUnreach) {
            Log ("{0}，转入队列等下次触发补发" -f $why)
            Enqueue $cfg.Target $body $Kind
        } else {
            Log ("{0}，按 queue_when_unreachable=false 丢弃" -f $why)
        }
    } else {
        # 剩下的才是"可能是暂时性故障"：微信窗口没开、会话没定位到、输入框没找着……
        # 这些重试有意义，照旧排队，攒到 5 次失败放弃。
        Log ("发送失败，退出码 {0}，转入队列重试" -f $rc)
        Enqueue $cfg.Target $body $Kind
    }
    exit 0
} finally {
    if ($owned) { try { $nMutex.ReleaseMutex() } catch {} }
    if ($nMutex) { $nMutex.Dispose() }
    # payload 是 hook 一次性的临时文件，必须删：总开关关闭时每回合都会来一个。
    if ($PayloadFile -and (Test-Path -LiteralPath $PayloadFile)) {
        try { Remove-Item -LiteralPath $PayloadFile -Force -ErrorAction SilentlyContinue } catch {}
    }
}
