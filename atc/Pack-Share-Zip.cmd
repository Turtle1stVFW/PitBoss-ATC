@echo off
REM Host operator: zip a tester copy with config / secrets stripped.
cd /d "%~dp0"
call "%~dp0_find_python.cmd"
if errorlevel 1 exit /b 1
"%ATC_PYTHON%" "%~dp0pack_share.py"
echo.
pause
exit /b %ERRORLEVEL%
