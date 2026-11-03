@echo off
rem 1) packaged exe first (no Python needed)
if exist "%~dp0dist\syspet.exe" (
  start "" "%~dp0dist\syspet.exe"
  exit /b
)
rem 2) project venv (has PySide6)
if exist "%~dp0.buildenv\Scripts\pythonw.exe" (
  start "" "%~dp0.buildenv\Scripts\pythonw.exe" "%~dp0app.py"
  exit /b
)
rem 3) legacy tkinter UI (runs on any Python)
start "" pythonw "%~dp0syspet.py"
