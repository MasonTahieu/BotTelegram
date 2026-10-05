@echo off
cd /d "%~dp0"
echo Catching up closed daily historical data incrementally: KBS with Vietcap fallback.
echo Checkpoint/resume enabled. Press Ctrl+C to stop safely.
call "%~dp0_python.cmd" -B -m fintech_bot.data.vnstock_sync --config configs\vietcap.toml %*
pause
