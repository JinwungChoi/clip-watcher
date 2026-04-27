@echo off
REM Launches clip_watcher.py silently (no console window) via pythonw.
cd /d "%~dp0"
start "" pythonw "%~dp0clip_watcher.py"
