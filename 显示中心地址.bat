@echo off
REM ============================================================
REM  Show this PC's LAN address + the exact command for the Pi.
REM  (All text here is ASCII on purpose: cmd.exe mangles UTF-8
REM   without a chcp. The Chinese output comes from the .ps1.)
REM ============================================================
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\show-center-address.ps1"

echo.
pause
