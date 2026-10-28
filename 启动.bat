@echo off
rem Use pythonw from PATH if available, else fall back to local anaconda (edit path as needed)
where pythonw >nul 2>&1 && (
  start "" pythonw "%~dp0syspet.py"
) || (
  start "" "D:\ruanjian\anaconda\pythonw.exe" "%~dp0syspet.py"
)
