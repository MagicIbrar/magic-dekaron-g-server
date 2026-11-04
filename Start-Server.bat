@echo off
REM Dekaron G server launcher (Windows, double-click). Runs the PowerShell launcher which reads server.config.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File start-server.ps1
pause
