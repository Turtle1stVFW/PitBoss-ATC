@echo off
REM Idempotent DCS Export install for the ATC frequency gate.
REM Safe for Setup UI, first-run, or a Windows installer custom action:
REM   Install-DCS-Radio-Export.cmd
REM   Install-DCS-Radio-Export.cmd --status
REM   Install-DCS-Radio-Export.cmd --uninstall
cd /d "%~dp0"
py -3 "%~dp0install_dcs_radio_export.py" %*
exit /b %ERRORLEVEL%
