@echo off
REM Host operator: build PitBossATC-Setup.exe with Python, numpy, and Whisper inside.
cd /d "%~dp0"
call "%~dp0_find_python.cmd"
if errorlevel 1 exit /b 1
"%ATC_PYTHON%" "%~dp0installer\build_beta.py" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
