@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0_python.cmd" -B -m fintech_bot chat
pause
