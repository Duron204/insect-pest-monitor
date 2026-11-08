@echo off
REM ============================================================
REM  Pest and Disease Monitoring - Build frontend for production
REM  Output: vue-frontend\dist  (then served by the backend at :8000)
REM ============================================================
cd /d "%~dp0vue-frontend"

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm not found. Please install Node.js 20 or newer.
  pause
  exit /b 1
)

if not exist node_modules (
  echo [1/2] Installing frontend dependencies...
  call npm install
  if errorlevel 1 (
    echo [ERROR] npm install failed.
    pause
    exit /b 1
  )
)

echo [2/2] Building, runs TypeScript type-check first...
call npm run build
if errorlevel 1 (
  echo [ERROR] Build failed. See the errors above.
  pause
  exit /b 1
)

echo.
echo Build finished: vue-frontend\dist
echo Now just run start_backend.bat and open http://127.0.0.1:8000
pause
