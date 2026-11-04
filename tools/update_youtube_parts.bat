@echo off
rem Brings the YouTube parts (yt-dlp, yt-dlp-ejs, Deno) up to date before a release.
rem   tools\update_youtube_parts.bat           shows what is new (changes nothing)
rem   tools\update_youtube_parts.bat --write   writes the new versions and fingerprints
rem Then run the tests and make a release: friends only get what you signed.
setlocal
cd /d "%~dp0.."
python tools\update_youtube_parts.py %*
if errorlevel 1 pause
endlocal
