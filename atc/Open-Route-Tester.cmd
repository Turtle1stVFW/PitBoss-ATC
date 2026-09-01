@echo off
REM Same local map server as the zone editor; opens the /tester page.
REM --replace clears a stale editor left on the port after updates.
cd /d "%~dp0"
call "%~dp0_find_python.cmd"
if errorlevel 1 exit /b 1
start "ATC Route Tester" "%ATC_PYTHON%" "%~dp0..\tools\zone_server.py" --replace --page tester
