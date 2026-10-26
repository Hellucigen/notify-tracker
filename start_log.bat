@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not defined PY set "PY=python"
title Notify Tracker - 监听中
"%PY%" notify_log.py %*
pause
