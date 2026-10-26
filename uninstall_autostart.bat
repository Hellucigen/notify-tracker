@echo off
chcp 65001 >nul
title 取消开机自启
set "TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\start_log_background.vbs"
if exist "%TARGET%" (
  del "%TARGET%"
  echo 已删除开机自启项：%TARGET%
) else (
  echo 没有找到开机自启项（本来就没设置）
)
pause
