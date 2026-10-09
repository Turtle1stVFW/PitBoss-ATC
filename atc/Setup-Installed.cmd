@echo off
REM Post-install step for PitBossATC-Setup.exe. Not the everyday launcher.
cd /d "%~dp0"
set "PYTHONNOUSERSITE=1"
if not exist "%~dp0runtime\python.exe" (
  echo Bundled Python is missing from this install.
  exit /b 1
)
"%~dp0runtime\python.exe" "%~dp0setup_pilot.py" --skip-whisper-warm --from-installer
exit /b %ERRORLEVEL%
