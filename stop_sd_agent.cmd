@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "logs\sd_agent.pid" (
  echo SD-агент не запущен: нет файла logs\sd_agent.pid
  pause
  exit /b 1
)
set /p APID=<"logs\sd_agent.pid"
echo Останавливаю SD-агент ^(PID %APID%^)...
taskkill /T /F /PID %APID% >nul 2>&1
if errorlevel 1 (
  echo Не удалось остановить — возможно, процесс уже завершён.
) else (
  echo Готово. Опрос ServiceDesk остановлен.
)
del /q "logs\sd_agent.pid" >nul 2>&1
pause
