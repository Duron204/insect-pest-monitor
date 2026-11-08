@echo off
REM ============================================================
REM  Pest and Disease Monitoring - Frontend dev server (Vite)
REM  URL: http://127.0.0.1:3000
REM  Start start_backend.bat first: Vite proxies /api /media /ws to 8000.
REM  For single-port production use build_frontend.bat + start_backend.bat.
REM ============================================================
cd /d "%~dp0vue-frontend"

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm not found. Please install Node.js 20 or newer.
  pause
  exit /b 1
)

if not exist node_modules (
  echo Installing frontend dependencies, this may take a few minutes...
  call npm install
  if errorlevel 1 (
    echo [ERROR] npm install failed.
    pause
    exit /b 1
  )
)

echo Starting frontend at http://127.0.0.1:3000
call npm run dev
pause
