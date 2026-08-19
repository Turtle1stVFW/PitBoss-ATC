@echo off
cd /d "%~dp0"
call "%~dp0_find_python.cmd"
if errorlevel 1 exit /b 1
"%ATC_PYTHON%" fake_pilots.py %*
echo.
pause
