@echo off
REM --replace clears a stale editor left on the port after updates (empty Packs list).
cd /d "%~dp0"
call "%~dp0_find_python.cmd"
if errorlevel 1 exit /b 1
start "ATC Zone Editor" "%ATC_PYTHON%" "%~dp0..\tools\zone_server.py" --replace
