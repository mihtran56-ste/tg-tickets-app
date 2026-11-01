@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal enabledelayedexpansion

if not exist ".venv\Scripts\python.exe" (
  echo Python venv not found: .venv\Scripts\python.exe
  echo Create it first: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

set FAILED=0
set TOTAL=0

for %%f in (tests\test_*.py) do (
  set /a TOTAL+=1
  echo === %%~nf
  .venv\Scripts\python.exe "%%f" > ".check.log" 2>&1
  if errorlevel 1 (
    echo    FAILED
    set FAILED=1
    findstr /C:"AssertionError" /C:"Traceback" /C:"Error" ".check.log" | more
  ) else (
    echo    OK
  )
)

del /q ".check.log" >nul 2>&1

echo.
echo ================================
if "%FAILED%"=="1" (
  echo RESULT: some checks FAILED - see output above
) else (
  echo RESULT: all %TOTAL% test files passed
)
echo ================================
echo.
pause
