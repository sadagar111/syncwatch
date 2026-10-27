@echo off
rem Opens two SyncWatch windows on this PC with separate profiles ("TestA" and "TestB"),
rem so you can test watching together without a second computer.
rem In window A click Host; in window B type 127.0.0.1 and click Join.
rem Tip: mute one window. The profiles keep their own settings, so you only set them up once.
rem Works in the source-code folder (tools\ next to SyncWatch.bat) and in the installed
rem version (app\tools\, two levels below SyncWatch.bat).
set "LAUNCHER=%~dp0..\SyncWatch.bat"
if not exist "%LAUNCHER%" set "LAUNCHER=%~dp0..\..\SyncWatch.bat"
start "SyncWatch test A" cmd /c ""%LAUNCHER%" --profile TestA"
timeout /t 4 /nobreak >nul
start "SyncWatch test B" cmd /c ""%LAUNCHER%" --profile TestB"
