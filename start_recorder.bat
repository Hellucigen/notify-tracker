@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not defined PY set "PY=python"
title Notify Tracker - 录屏
rem 需要第三方依赖：pip install mss opencv-python
rem 常用参数： --preview 先看范围   --motion 只在画面变化时录   --scale 0.75 更省磁盘
"%PY%" notify_recorder.py %*
pause
