@echo off
rem 后台运行通知记录器：无控制台窗口，输出与报错写进 logs\runner.log
cd /d "%~dp0"
if not defined PYW set "PYW=pythonw"
if not exist logs mkdir logs
"%PYW%" notify_log.py --quiet >> logs\runner.log 2>&1
