@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog.ps1"
