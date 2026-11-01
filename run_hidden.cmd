@echo off
echo marker > "C:\Users\mihtr\Desktop\proekt\TG_Tickets\logs\launcher_ran.txt"
start "" powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\Users\mihtr\Desktop\proekt\TG_Tickets\watchdog.ps1"
