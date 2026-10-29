@echo off
rem How long do jumps take on this PC? Drag a film onto this file, or:
rem   tools\measure_jumps.bat "D:\Films\Some film.mkv"
rem Plays the film without sound or window for about half a minute, jumps a few times,
rem and prints a summary to send back. Nothing is saved or changed.
setlocal
set "PY=python"
set "ROOT=%~dp0..\.."
if exist "%ROOT%\runtime\current.txt" (
  set /p RT=<"%ROOT%\runtime\current.txt"
)
if defined RT if exist "%ROOT%\runtime\%RT%\python.exe" set "PY=%ROOT%\runtime\%RT%\python.exe"
if "%~1"=="" (
  echo Drag a film onto measure_jumps.bat, or run: measure_jumps.bat "path\to\film.mkv"
  pause
  exit /b 1
)
"%PY%" "%~dp0measure_jumps.py" "%~1"
pause
