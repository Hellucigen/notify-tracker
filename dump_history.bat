@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not defined PY set "PY=python"
title 查看通知历史
"%PY%" notify_log.py --dump 40
pause
