# Does a spawned child outlive its parent when the parent exits immediately?
# Parent spawns child, child sleeps 6s then writes a marker, parent exits at once.
$ErrorActionPreference = 'SilentlyContinue'
$mark = Join-Path $PSScriptRoot '_detach_marker.txt'
Remove-Item -LiteralPath $mark -Force -ErrorAction SilentlyContinue
$child = Join-Path $PSScriptRoot '_detach_child.ps1'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($child,
    "Start-Sleep -Seconds 6`r`n[System.IO.File]::WriteAllText('$($mark -replace '\\','\\')', (Get-Date).ToString('o'))`r`n",
    $utf8)
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
$psi.Arguments = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $child)
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$p = [System.Diagnostics.Process]::Start($psi)
Write-Output ("spawned child pid={0}; parent exiting now" -f $p.Id)
exit 0
