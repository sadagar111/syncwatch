@echo off
rem Fake viewers for testing your room with many people. Usage:
rem   tools\bots.bat <room code> [--count 9] [--lag-ms 500] [--drift] [--chaos]
rem Copy the code with "Copy code" in SyncWatch, then let the bots in there.
rem Ctrl+C ends. Details: python tools\bots.py --help
cd /d "%~dp0\.."
if "%~1"=="" (
  python tools\bots.py --help
  pause
  exit /b 1
)
python tools\bots.py %*
