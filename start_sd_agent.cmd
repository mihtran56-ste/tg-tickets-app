@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Не найден python: .venv\Scripts\python.exe
  pause
  exit /b 1
)
echo Запускаю SD-агент скрыто (лог: logs\sd_agent.log)...
start "" powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0sd_agent.ps1"
timeout /t 3 >nul
echo Готово. Проверить: logs\sd_agent.log
