param(
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir
$configPath = Join-Path $projectDir "config\feishu_claude_bot.v2.json"
$botPath = Join-Path $projectDir "app\feishu_claude_bot.py"
$bootstrapPath = Join-Path $projectDir "app\bootstrap_feishu_tool.py"

if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Missing config file: $configPath"
}

if (-not (Test-Path -LiteralPath $bootstrapPath)) {
    throw "Missing bootstrap file: $bootstrapPath"
}

$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json

# Use ASCII-only guardrails so PowerShell parsing stays stable across encodings.
if ([string]::IsNullOrWhiteSpace($config.app_id) -or $config.app_id -like "cli_x*") {
    throw "Please set a real app_id in $configPath"
}

if ([string]::IsNullOrWhiteSpace($config.app_secret) -or $config.app_secret -eq 'your-feishu-app-secret') {
    throw "Please set a real app_secret in $configPath"
}

if (-not (Test-Path -LiteralPath $config.claude_path)) {
    throw "claude.exe not found: $($config.claude_path)"
}

if (-not (Test-Path -LiteralPath $config.default_cwd)) {
    throw "default_cwd not found: $($config.default_cwd)"
}

$hasSdk = python -c "import sys; from pathlib import Path; sys.path.insert(0, str(Path(r'$projectDir') / 'vendor')); import importlib.util; print(importlib.util.find_spec('lark_oapi') is not None)"
if ($hasSdk.Trim() -ne "True") {
    # Keep pip install as a fallback, but the normal path now uses the bundled vendor copy after the directory consolidation.
    Write-Host "Installing lark-oapi..."
    python -m pip install lark-oapi
}

if ($ValidateOnly) {
    Write-Host "Feishu Claude Bot config validation passed."
    return
}

Write-Host "Starting Feishu Claude Bot..."
python $bootstrapPath $botPath --config $configPath
