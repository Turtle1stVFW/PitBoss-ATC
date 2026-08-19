@echo off
cd /d "%~dp0"
py -3 fake_pilots.py %*
echo.
pause
