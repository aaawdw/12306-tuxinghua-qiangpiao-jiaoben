@echo off
setlocal
cd /d "%~dp0"
title 12306 Ticket Assistant
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0main.py"
) else (
  python "%~dp0main.py"
)
if errorlevel 1 pause
