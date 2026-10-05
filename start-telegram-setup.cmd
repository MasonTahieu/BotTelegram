@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Dang mo cua so nhap token Telegram...
call "%~dp0_python.cmd" -B -m fintech_bot setup-telegram
if errorlevel 1 pause
