@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Не найден .venv\Scripts\pythonw.exe — сначала создай окружение
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0keepalive.py"
