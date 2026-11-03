@echo off
rem LiveMax server manager - double-click to open the menu.
rem Runs livemax.ps1 in this folder (ExecutionPolicy Bypass: needed for files downloaded as ZIP).
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0livemax.ps1" %*
echo.
pause
