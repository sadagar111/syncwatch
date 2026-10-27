@echo off
rem Publishing SyncWatch and managing your signing key. Examples:
rem   tools\release.bat keygen
rem   tools\release.bat publish --dry-run
rem   tools\release.bat publish
rem Run it in the VS Code terminal from the project folder.
cd /d "%~dp0\.."
python tools\release.py %*
