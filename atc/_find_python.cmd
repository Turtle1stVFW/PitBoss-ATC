@echo off
REM Locate a desktop Python 3 interpreter (tkinter required for the Host UI).
REM CALL this file from other .cmd launchers. Do not use setlocal here —
REM the caller needs ATC_PYTHON in its environment.
REM
REM Optional: set ATC_PYTHON_OVERRIDE to a full python.exe path.

set "ATC_PYTHON="

if defined ATC_PYTHON_OVERRIDE (
  if exist "%ATC_PYTHON_OVERRIDE%" (
    set "ATC_PYTHON=%ATC_PYTHON_OVERRIDE%"
    goto :check
  )
)

REM 1) Official py launcher (often missing on dedicated servers)
where py >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
    if exist "%%~I" set "ATC_PYTHON=%%~I"
  )
)
if defined ATC_PYTHON goto :check

REM 2) python.exe on PATH — skip the Microsoft Store stub
where python >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%I in ('where python 2^>nul') do (
    echo %%I | find /i "\WindowsApps\" >nul
    if errorlevel 1 (
      if exist "%%~I" (
        set "ATC_PYTHON=%%~I"
        goto :check
      )
    )
  )
)

where python3 >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%I in ('where python3 2^>nul') do (
    echo %%I | find /i "\WindowsApps\" >nul
    if errorlevel 1 (
      if exist "%%~I" (
        set "ATC_PYTHON=%%~I"
        goto :check
      )
    )
  )
)

REM 3) Standard install folders (PATH not required)
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do (
  if exist "%%~D\python.exe" (
    set "ATC_PYTHON=%%~D\python.exe"
    goto :check
  )
)
for /d %%D in ("%ProgramFiles%\Python3*") do (
  if exist "%%~D\python.exe" (
    set "ATC_PYTHON=%%~D\python.exe"
    goto :check
  )
)
set "ATC_PF86=%ProgramFiles(x86)%"
if defined ATC_PF86 (
  for /d %%D in ("%ATC_PF86%\Python3*") do (
    if exist "%%~D\python.exe" (
      set "ATC_PYTHON=%%~D\python.exe"
      goto :check
    )
  )
)
for /d %%D in ("%SystemDrive%\Python3*" "%SystemDrive%\Python*") do (
  if exist "%%~D\python.exe" (
    set "ATC_PYTHON=%%~D\python.exe"
    goto :check
  )
)

REM 4) Optional private runtime next to this folder
if exist "%~dp0runtime\python.exe" (
  set "ATC_PYTHON=%~dp0runtime\python.exe"
  goto :check
)

goto :missing

:check
if not defined ATC_PYTHON goto :missing
"%ATC_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo.
  echo Found Python at:
  echo   %ATC_PYTHON%
  echo but it is older than 3.10.
  echo.
  pause
  exit /b 1
)
"%ATC_PYTHON%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
  echo.
  echo Found Python at:
  echo   %ATC_PYTHON%
  echo but it cannot import tkinter ^(the Host GUI toolkit^).
  echo.
  echo Reinstall from https://www.python.org/downloads/windows/
  echo and enable "tcl/tk and IDLE" plus "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)
exit /b 0

:missing
echo.
echo Windows cannot find Python 3.
echo The "py" launcher is not required — python.exe is enough.
echo.
echo Install desktop Python on THIS PC ^(pilot or Host^):
echo   https://www.python.org/downloads/windows/
echo.
echo In the installer, check:
echo   - Add python.exe to PATH
echo   - tcl/tk and IDLE
echo   - py launcher  ^(optional^)
echo Then run this .cmd again.
echo.
echo To point at an existing install:
echo   set ATC_PYTHON_OVERRIDE=C:\Path\to\python.exe
echo.
pause
exit /b 1
