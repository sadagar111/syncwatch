@echo off
rem Opens two SyncWatch windows on this PC with separate profiles ("TestA" and "TestB"),
rem so you can try rooms without a second computer. Each profile is a different person
rem (its own ID), and they keep their own settings, so you only set them up once.
rem   Window A: Host my room, then Copy code.
rem   Window B: paste the code under "Join a room", click Join.
rem   Window A: click "Let in".
rem Tip: mute one window. For more people, use tools\bots.bat.
rem Works in the source-code folder (tools\ next to SyncWatch.bat) and in the installed
rem version (app\tools\, two levels below SyncWatch.bat).
set "LAUNCHER=%~dp0..\SyncWatch.bat"
if not exist "%LAUNCHER%" set "LAUNCHER=%~dp0..\..\SyncWatch.bat"
start "SyncWatch test A" cmd /c ""%LAUNCHER%" --profile TestA"
timeout /t 4 /nobreak >nul
start "SyncWatch test B" cmd /c ""%LAUNCHER%" --profile TestB"
