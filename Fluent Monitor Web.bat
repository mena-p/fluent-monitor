@echo off
rem Double-click: serves the last folder used to browsers (phone, tablet, other PCs).
rem Drag a case folder or a .out file onto this file to serve that folder. Ctrl+C stops it.
rem The first start downloads Python + dependencies into this project's .venv (needs internet once).
where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found. Install it first, see README.md:
    echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    pause
    exit /b 1
)
uv run --project "%~dp0." --locked fluent-monitor-web %*
pause
