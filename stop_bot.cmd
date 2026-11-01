@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "logs\watchdog.pid" (
  echo Сторож не запущен: нет файла logs\watchdog.pid
  pause
  exit /b 1
)
set /p WPID=<"logs\watchdog.pid"
echo Останавливаю сторожа и бота ^(PID %WPID%^)...
taskkill /T /F /PID %WPID% >nul 2>&1
if errorlevel 1 (
  echo Не удалось остановить — возможно, процесс уже завершён.
) else (
  echo Готово. Мониторинг остановлен.
)
del /q "logs\watchdog.pid" >nul 2>&1
pause
