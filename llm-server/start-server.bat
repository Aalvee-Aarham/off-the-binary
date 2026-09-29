@echo off
rem Double-click to start the LLM server locally (http://127.0.0.1:8100/ui). Close the window to stop it.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\run-local.ps1" %*
pause
