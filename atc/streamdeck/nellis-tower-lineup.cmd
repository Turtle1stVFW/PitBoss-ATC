@echo off
call "%~dp0..\_find_python.cmd"
if errorlevel 1 exit /b 1
start "" /min "%ATC_PYTHON%" "%~dp0..\atc_phrase.py" --airport nellis --role tower --phrase lineup
