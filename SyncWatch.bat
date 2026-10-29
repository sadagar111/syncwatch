@echo off
setlocal
cd /d "%~dp0"
title SyncWatch

rem ==== Installed version: SyncWatch brings its own Python (runtime folder) ==================
rem Started without a console window. "SyncWatch.bat --console" shows one, for troubleshooting.
if not exist "runtime\current.txt" goto source_version
set /p RT=<"runtime\current.txt"
if not exist "runtime\%RT%\pythonw.exe" goto broken_runtime
if not exist "boot.py" goto broken_runtime
rem (Inside the brackets, %2..%9 are the arguments after --console.)
if /i "%~1"=="--console" (
  "runtime\%RT%\python.exe" boot.py %2 %3 %4 %5 %6 %7 %8 %9
  pause
  exit /b
)
start "" "runtime\%RT%\pythonw.exe" boot.py %*
exit /b 0

:broken_runtime
echo.
echo  SW-713  SyncWatch's own Python is missing or damaged.
echo.
echo  Download the full SyncWatch zip again and unzip it over this folder.
echo  Your settings are kept: they're stored separately.
echo.
pause
exit /b 1

rem ==== Source-code version (a Git folder): uses the Python installed on this PC =============
:source_version
rem ---- 1. Find Python: "python" first, then the "py" launcher --------------------------------
rem (A Microsoft Store "python" shortcut without real Python fails this test, as it should.)
set "PY="
python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY goto no_python

rem ---- 2. Python version ---------------------------------------------------------------------
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" || goto old_python

rem ---- 3. Install the libraries, only if something is missing --------------------------------
rem (mpv is only looked for here, not loaded: its DLL comes in step 3b.)
%PY% -c "import PySide6.QtMultimedia, cryptography, av, importlib.util as u; assert u.find_spec('mpv')" >nul 2>&1
if errorlevel 1 (
  echo Installing the libraries SyncWatch needs ^(PySide6, cryptography, mpv, PyAV^). This happens only once...
  %PY% -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto install_failed
)

rem ---- 3b. The video player library (libmpv-2.dll), only if it's missing ----------------------
rem If this fails, SyncWatch still starts, with its previous video player (SW-209).
if not exist "libmpv\libmpv-2.dll" %PY% tools\get_libmpv.py

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
echo  SW-104  Couldn't install what SyncWatch needs (PySide6, cryptography).
echo.
echo  What you can try:
echo   - Check your internet connection.
echo   - If your Python is brand new (3.15 or newer), install Python 3.13 instead:
echo     the libraries aren't available for brand-new Python versions straight away.
echo   - Antivirus or a school/work network can block downloads. Try another network.
echo.
pause
exit /b 1
