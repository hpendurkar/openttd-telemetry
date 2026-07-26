@echo off
REM Watches the standard OpenTTD autosave folder and extracts telemetry
REM CSVs + a map screenshot for every new autosave, until Ctrl+C.
REM See CLAUDE.MD / README.md for what gets extracted and why.

cd /d "%~dp0"
python openttd_telemetry.py --watch-dir "%USERPROFILE%\Documents\OpenTTD\save\autosave" --out-dir "%~dp0extracted_data" --poll-seconds 30

pause
