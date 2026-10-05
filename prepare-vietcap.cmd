@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Chuẩn bị dữ liệu tài chính Vietcap cho bộ lọc cơ bản.
call "%~dp0_python.cmd" -B -m fintech_bot.data.vietcap_sync --config configs\vietcap.toml --components ratios income --resume --max-age 604800 --timeout 30 --workers 1 --interval 2 %*
pause
