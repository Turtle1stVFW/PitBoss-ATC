@echo off
REM First time on a pilot or Host PC: Python check, config, voice deps, DCS export.
cd /d "%~dp0"
call "%~dp0_find_python.cmd"
if errorlevel 1 exit /b 1
"%ATC_PYTHON%" "%~dp0setup_pilot.py" %*
if errorlevel 1 (
  echo.
  echo Setup did not finish cleanly. Fix the FAIL line above and run this again.
  pause
  exit /b 1
)
echo Starting the app…
start "ATC Flow" "%ATC_PYTHON%" "%~dp0flow_ui.py"
exit /b 0
