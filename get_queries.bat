@echo off
setlocal
chcp 65001 >nul

echo Starting flnd Twitter/X profile and query downloads...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get_queries.ps1"

echo.
echo Execution finished.
pause
