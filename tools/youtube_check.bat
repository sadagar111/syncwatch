@echo off
rem Checks which ways of asking YouTube work on this PC (for SW-609 "Couldn't play the YouTube video").
rem Changes nothing. Prints a table and saves it as youtube_check.txt in SyncWatch's log folder.
rem   tools\youtube_check.bat                  with a short test video
rem   tools\youtube_check.bat [YouTube link]   with that video
setlocal
cd /d "%~dp0.."
set "PY="
rem The zip for friends brings its own Python; the source-code version uses the one on this PC.
if not exist "runtime\current.txt" goto source_version
set /p RT=<"runtime\current.txt"
if exist "runtime\%RT%\python.exe" set "PY=runtime\%RT%\python.exe"
goto run
:source_version
python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
:run
if not defined PY (
  echo Python wasn't found. Start SyncWatch.bat once first, then try again.
  pause
  exit /b 1
)
%PY% tools\youtube_check.py %*
echo.
pause
endlocal
