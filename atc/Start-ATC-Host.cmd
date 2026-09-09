@echo off
REM Same as Open-ATC-Setup.cmd — named for the dedicated-server Host box.
cd /d "%~dp0"
call "%~dp0_find_python.cmd"
if errorlevel 1 exit /b 1
start "PitBoss ATC Host" "%ATC_PYTHON%" "%~dp0flow_ui.py"
