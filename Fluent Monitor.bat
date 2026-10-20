@echo off
rem Fluent monitor: starts the monitor and opens it in your browser.
rem   Double-click: the folder you used last (switch folders in the page with "Change folder...").
rem   Drag a case folder or a .out file onto this file: watch that folder.
rem Keep this window open while you use the page; closing it stops the monitor.
rem To reach it from your phone on the same network, add --lan at the end of the "uv run" line.
rem The first start downloads Python + dependencies into this folder's .venv (needs internet once).
title Fluent monitor
where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found. Install it first, see README.md:
    echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    pause
    exit /b 1
)
uv run --project "%~dp0." --locked fluent-monitor %*
if errorlevel 1 pause
