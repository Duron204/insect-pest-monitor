@echo off
REM ============================================================
REM  Pest and Disease Monitoring - Start backend + frontend (dev)
REM  Frontend: http://127.0.0.1:3000
REM  Backend : http://127.0.0.1:8000
REM  Single-port mode: run build_frontend.bat, then start_backend.bat
REM  only, and open http://127.0.0.1:8000
REM ============================================================

echo Starting the backend in a separate window...
start "insect-backend" cmd /k "%~dp0start_backend.bat"

REM Wait for the backend to actually answer instead of guessing a fixed delay.
REM Opening the frontend too early is what produces HTTP 502 on every API call.
where curl.exe >nul 2>nul
if errorlevel 1 (
  echo [WARN] curl.exe not found, skipping the backend health check.
  goto startfront
)

echo Waiting for the backend to answer on /api/health ...
echo The model is preloaded on startup, this can take 10-30 seconds.
set /a TRIES=0
:waitloop
set /a TRIES+=1
curl.exe -s -o nul --max-time 3 http://127.0.0.1:8000/api/health
if not errorlevel 1 goto ready
if %TRIES% GEQ 40 goto notready
ping -n 3 127.0.0.1 >nul
goto waitloop

:ready
echo Backend is ready.

:startfront
echo Starting the frontend in a separate window...
start "insect-frontend" cmd /k "%~dp0start_frontend.bat"

echo.
echo ============================================================
echo   Frontend : http://127.0.0.1:3000
echo   API docs : http://127.0.0.1:8000/docs
echo.
echo   Register a new account on the login page, or migrate the old
echo   desktop accounts first:
echo     backend\migrations\migrate_users.py
echo ============================================================
pause
goto :eof

:notready
echo.
echo [WARNING] The backend did not answer within about 80 seconds.
echo           Look at the "insect-backend" window for the real error.
echo           Common causes:
echo             - port 8000 is already used by another program
echo             - the Python path inside start_backend.bat is wrong
echo.
echo           The frontend will still start, but every API call will
echo           fail with HTTP 502 until the backend is running.
echo.
goto startfront
