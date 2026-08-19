@echo off
REM Idempotent DCS Export install for the ATC frequency gate.
REM Safe for Setup UI, first-run, or a Windows installer custom action:
REM   Install-DCS-Radio-Export.cmd
REM   Install-DCS-Radio-Export.cmd --status
REM   Install-DCS-Radio-Export.cmd --uninstall
cd /d "%~dp0"
call "%~dp0_find_python.cmd"
if errorlevel 1 exit /b 1
"%ATC_PYTHON%" "%~dp0install_dcs_radio_export.py" %*
exit /b %ERRORLEVEL%
