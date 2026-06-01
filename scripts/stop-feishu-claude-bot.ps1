$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir
$botPath = Join-Path $projectDir "app\feishu_claude_bot.py"

$botPathNorm = $botPath.Replace('\', '/').ToLower()
$found = $false

Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
    $cmdLine = $_.CommandLine
    if ($cmdLine -and ($cmdLine.Replace('\', '/').ToLower() -match [regex]::Escape($botPathNorm))) {
        Write-Host "Stopping bot (PID: $($_.ProcessId), CommandLine: $cmdLine)"
        Stop-Process -Id $_.ProcessId -Force
        $found = $true
    }
}

if ($found) {
    Write-Host "Feishu Claude Bot stopped."
} else {
    Write-Host "Feishu Claude Bot is not running."
}
