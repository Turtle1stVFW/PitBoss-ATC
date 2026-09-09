@echo off
REM Same app as Open-Flight-Flow.cmd. First-time testers should run Setup-Pilot.cmd.
cd /d "%~dp0"
call "%~dp0_find_python.cmd"
if errorlevel 1 exit /b 1
start "PitBoss ATC" "%ATC_PYTHON%" "%~dp0flow_ui.py"
