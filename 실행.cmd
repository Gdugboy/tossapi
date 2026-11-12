@echo off
setlocal
cd /d "%~dp0"
if exist "tossapi\launch.py" cd /d "%~dp0tossapi"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" launch.py
) else (
    py -3 launch.py
)
if errorlevel 1 pause
