@echo off
REM Processes every new autosave in the standard OpenTTD autosave folder
REM (telemetry CSVs + a map screenshot each), then exits - run this again
REM whenever you want to catch up, rather than leaving it running.
REM See CLAUDE.MD / README.md for what gets extracted and why.

cd /d "%~dp0"
python openttd_telemetry.py --watch-dir "%USERPROFILE%\Documents\OpenTTD\save\autosave" --out-dir "%~dp0extracted_data" --once

pause
