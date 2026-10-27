@echo off
rem Lets SyncWatch accept connections. Run once on the PC that hosts (admin prompt will appear).
net session >nul 2>&1 || (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
echo Removing Windows Firewall rules that BLOCK python.exe (created when the firewall popup was dismissed)...
powershell -NoProfile -Command "Get-NetFirewallRule -Direction Inbound -Action Block | Where-Object { ($_ | Get-NetFirewallApplicationFilter).Program -match 'pythonw?\.exe$' } | ForEach-Object { Write-Host ('  removed: ' + $_.DisplayName); $_ | Remove-NetFirewallRule }"
echo Allowing TCP port 50505 on all network types...
powershell -NoProfile -Command "Remove-NetFirewallRule -DisplayName 'SyncWatch' -ErrorAction SilentlyContinue; New-NetFirewallRule -DisplayName 'SyncWatch' -Direction Inbound -Protocol TCP -LocalPort 50505 -Action Allow -Profile Any | Out-Null"
echo.
echo Done. Click Host in SyncWatch again and have your friend Join.
pause
