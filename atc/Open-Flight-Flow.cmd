@echo off
cd /d "%~dp0"
call "%~dp0_find_python.cmd"
if errorlevel 1 exit /b 1
start "ATC Flow" "%ATC_PYTHON%" "%~dp0flow_ui.py"
