@echo off
setlocal
rem Resolve paths from this script so the public template does not expose a local machine layout.
set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."
if "%FEISHU_CLAUDE_PYTHON%"=="" set "FEISHU_CLAUDE_PYTHON=python"
"%FEISHU_CLAUDE_PYTHON%" "%PROJECT_DIR%\app\bootstrap_feishu_tool.py" "%PROJECT_DIR%\hooks\feishu_claude_permission_hook.py"
