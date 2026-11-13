@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.11 -m venv .venv
    if errorlevel 1 (
        echo Please install Python 3.11 or newer, then retry.
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" -c "import cv2, numpy, PIL, mss" >nul 2>nul
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" main.py
if errorlevel 1 pause
