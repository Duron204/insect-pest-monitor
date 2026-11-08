@echo off
REM ============================================================
REM  Pest and Disease Monitoring - Backend (FastAPI)
REM  API docs: http://127.0.0.1:8000/docs
REM  Binds 0.0.0.0 so the Raspberry Pi / phone on the LAN can reach the center.
REM ============================================================
cd /d "%~dp0backend"

REM 优先用项目内 .venv，其次用 mynve 环境，最后用 PATH 里的 python。
set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto :py_found
set "PY=D:\python\envs\mynve\python.exe"
if exist "%PY%" goto :py_found
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] No usable Python found.
  echo         Tried: .venv\Scripts\python.exe, D:\python\envs\mynve\python.exe, PATH
  pause
  exit /b 1
)
set "PY=python"
:py_found
echo Using Python: %PY%

REM Port pre-check. The most common cause of "the page shows 502" is a second
REM backend that failed to bind because another one already owns port 8000.
netstat -ano | findstr /c:"LISTENING" | findstr /c:":8000 " >nul 2>nul
if not errorlevel 1 (
  echo [WARNING] Port 8000 is already in use.
  echo           A backend may already be running - try opening
  echo           http://127.0.0.1:8000/docs directly.
  echo           Otherwise close that program and run this script again.
  echo.
)

echo Starting backend on port 8000 (accessible from the LAN).
echo Local: http://127.0.0.1:8000
echo The model is preloaded on first start, please wait a few seconds.
echo Press Ctrl+C to stop.
echo.

"%PY%" -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000

echo.
echo Backend stopped.
pause
