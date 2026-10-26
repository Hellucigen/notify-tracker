@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not defined PYW set "PYW=pythonw"
start "" "%PYW%" app.py
