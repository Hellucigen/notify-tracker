@echo off
chcp 65001 >nul
title 停止通知记录器
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -like '*notify_log.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Output ('STOPPED PID=' + $_.ProcessId) }"
echo.
echo 如需取消开机自启，运行 uninstall_autostart.bat
pause
