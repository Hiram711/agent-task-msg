param([Parameter(Mandatory = $true)][string]$File)
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$File = [System.IO.Path]::GetFullPath($File.Replace('/', '\'))
$err = $null; $tok = $null
[System.Management.Automation.Language.Parser]::ParseFile($File, [ref]$tok, [ref]$err) | Out-Null
if ($err.Count -eq 0) { Write-Output 'PARSE OK'; exit 0 }
Write-Output ("errors: {0}" -f $err.Count)
foreach ($e in $err) {
    Write-Output ("--- line {0} col {1}  id={2}" -f $e.Extent.StartLineNumber, $e.Extent.StartColumnNumber, $e.ErrorId)
    Write-Output ("    text : [{0}]" -f $e.Extent.Text)
    Write-Output ("    msg  : {0}" -f $e.Message)
}
exit 1
