@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not defined PY set "PY=python"
title 回补通知历史
rem 从通知数据库（含已被系统删除的记录）导出一份完整历史
"%PY%" -m toastcrumbs.cli export --format json -o recovered\toastcrumbs.json
"%PY%" -m toastcrumbs.cli export -o recovered\notifications.html
"%PY%" merge_history.py --exclude "Recorder test"
"%PY%" to_excel.py
pause
