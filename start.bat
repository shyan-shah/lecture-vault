@echo off
rem Opens Lecture Vault (starts it first if it isn't already running).
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" app.py
) else (
  start "" pythonw app.py
)
