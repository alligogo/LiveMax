@echo off
rem LiveMax 유튜브 측정기 — 더블클릭하면 시작, 끝낼 때 Ctrl+C (요약이 나옴)
chcp 65001 >nul
title LiveMax 유튜브 측정기
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py yt_monitor.py
  exit /b
)
where python >nul 2>nul
if errorlevel 1 (
  echo 파이썬이 없습니다. https://www.python.org 에서 설치하세요 ^(설치할 때 "Add python.exe to PATH" 체크^)
  pause
  exit /b
)
python yt_monitor.py
