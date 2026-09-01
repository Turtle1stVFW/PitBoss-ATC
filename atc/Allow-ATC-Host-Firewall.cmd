@echo off
REM Run ON the Host / dedicated server (right-click → Run as administrator).
netsh advfirewall firewall add rule name="DCS ATC Host 8766" dir=in action=allow protocol=TCP localport=8766 enable=yes profile=any
if errorlevel 1 (
  echo Failed — this window must be Run as administrator.
  pause
  exit /b 1
)
echo Inbound TCP 8766 is allowed. Clients still need an IP on the SAME LAN as this PC.
pause
