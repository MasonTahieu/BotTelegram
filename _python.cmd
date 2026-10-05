@echo off
setlocal
rem Pin this project to Python 3.11; keep other installed versions available.
if exist "%USERPROFILE%\.venv\Scripts\python.exe" (
  set "FINTECH_PYTHON_EXE=%USERPROFILE%\.venv\Scripts\python.exe"
  goto run_env
)
if exist "%~dp0.venv\Scripts\python.exe" (
  set "FINTECH_PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
  goto run_env
)
where py >nul 2>nul
if errorlevel 1 goto missing_python
py -3.11 %*
exit /b %errorlevel%

:run_env
"%FINTECH_PYTHON_EXE%" -B -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)"
if errorlevel 1 goto wrong_version
"%FINTECH_PYTHON_EXE%" %*
exit /b %errorlevel%

:wrong_version
echo ERROR: The selected environment must use Python 3.11: "%FINTECH_PYTHON_EXE%" 1>&2
exit /b 1

:missing_python
echo ERROR: Python 3.11 was not found. Install Python 3.11 or restore the shared environment. 1>&2
exit /b 1
