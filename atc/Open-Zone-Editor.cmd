@echo off
REM --replace clears a stale editor left on the port after updates (empty Packs list).
start "" py -3 "%~dp0..\tools\zone_server.py" --replace
