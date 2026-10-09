@echo off
REM Attach dist\PitBossATC-Setup-*.exe to a GitHub Release.
REM A draft does not update testers. Add --publish when the exe should go out.
cd /d "%~dp0"
call "%~dp0_find_python.cmd"
if errorlevel 1 exit /b 1
"%ATC_PYTHON%" "%~dp0installer\publish_release.py" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
