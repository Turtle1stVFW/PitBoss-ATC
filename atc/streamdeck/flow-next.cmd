@echo off
call "%~dp0..\_find_python.cmd"
if errorlevel 1 exit /b 1
"%ATC_PYTHON%" "%~dp0..\flow_engine.py" next
