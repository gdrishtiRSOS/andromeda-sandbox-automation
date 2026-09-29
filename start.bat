@echo off
rem Double-click to open the sandbox account page in your browser.
cd /d "%~dp0"
python -m webapp
if errorlevel 1 pause
