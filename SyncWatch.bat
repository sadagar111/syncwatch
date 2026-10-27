@echo off
setlocal
cd /d "%~dp0"
title SyncWatch

rem ---- 1. Find Python: "python" first (what version 0.1 used), then the "py" launcher -------
rem (A Microsoft Store "python" shortcut without real Python fails this test, as it should.)
set "PY="
python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY goto no_python

rem ---- 2. Python version ---------------------------------------------------------------------
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" || goto old_python

rem ---- 3. Install the video/window library, only if it's missing ------------------------------
%PY% -c "import PySide6.QtMultimedia" >nul 2>&1
if errorlevel 1 (
  echo Installing the video and window library ^(PySide6^). This happens only once and takes a few minutes...
  %PY% -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto install_failed
)

rem ---- 4. Start ----------------------------------------------------------------------------------
echo SyncWatch is running. You can minimise this window; closing it also closes SyncWatch.
%PY% -m syncwatch %*
set "CODE=%errorlevel%"
if "%CODE%"=="0" exit /b 0
if "%CODE%"=="2" exit /b 2
if "%CODE%"=="3" exit /b 3
echo.
echo  SyncWatch closed unexpectedly (exit code %CODE%).
echo  Start it again: it will show error SW-990 with a "Copy details" button.
echo  Send those details to whoever gave you SyncWatch.
pause
exit /b %CODE%

:no_python
echo.
echo  SW-101  Python wasn't found on this PC.
echo.
echo  What you can try:
echo   - Install Python 3.13 from https://www.python.org/downloads/
echo     and tick "Add python.exe to PATH" at the start of the installer.
echo   - Then start SyncWatch again.
echo.
pause
exit /b 1

:old_python
echo.
echo  SW-101  Python is too old. SyncWatch needs Python 3.9 or newer.
echo  Install Python 3.13 from https://www.python.org/downloads/ and start SyncWatch again.
echo.
pause
exit /b 1

:install_failed
echo.
echo  SW-104  Couldn't install what SyncWatch needs (PySide6).
echo.
echo  What you can try:
echo   - Check your internet connection.
echo   - If your Python is brand new (3.15 or newer), install Python 3.13 instead:
echo     the library isn't available for brand-new Python versions straight away.
echo   - Antivirus or a school/work network can block downloads. Try another network.
echo.
pause
exit /b 1
