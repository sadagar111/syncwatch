@echo off
rem Removes the "SyncWatch" Windows Firewall rule that allow_firewall.bat (SyncWatch 0.3 and older) added.
rem Since 0.4 SyncWatch only makes outgoing connections, so the rule isn't needed any more.
rem (Python block rules that allow_firewall.bat deleted are not restored; Windows asks again when needed.)
net session >nul 2>&1 || (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -Command "Remove-NetFirewallRule -DisplayName 'SyncWatch' -ErrorAction SilentlyContinue"
echo The SyncWatch firewall rule was removed (if it existed).
pause
