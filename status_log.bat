@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not defined PY set "PY=python"
title Notify Tracker 状态
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -like '*notify_log.py*' }; if ($p) { Write-Output ('RUNNING  PID=' + ($p.ProcessId -join ',')) } else { Write-Output 'NOT RUNNING' }"
"%PY%" status_log.py
"%PY%" to_excel.py
echo.
pause
