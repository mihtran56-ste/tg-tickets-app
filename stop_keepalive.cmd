@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "logs\keepalive.pid" (
  echo Будильник не запущен: нет файла logs\keepalive.pid
  pause
  exit /b 1
)
set /p KPID=<"logs\keepalive.pid"
echo Останавливаю будильник ^(PID %KPID%^)...
taskkill /T /F /PID %KPID% >nul 2>&1
del /q "logs\keepalive.pid" >nul 2>&1
echo Готово. Учтите: без будильника бесплатный сервис засыпает через ~15 минут.
pause
