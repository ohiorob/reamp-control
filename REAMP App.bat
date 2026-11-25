@echo off
rem REAMP live app - double-click to start. Close REAMP-Logger first.
cd /d "%~dp0"
py -3 reamp_app.py %*
if errorlevel 1 pause
