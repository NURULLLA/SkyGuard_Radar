@echo off
title Skyguard Radar - Timetable
color 0B
echo.
echo  ==============================================
echo    SKYGUARD RADAR - Flight Timetable
echo  ==============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.9+
    pause
    exit /b 1
)

echo [1/2] Installing dependencies...
pip install -r requirements.txt -q

echo [2/2] Starting server...
echo.
echo  Open http://localhost:5050
echo  Ctrl+C to stop
echo.
python app.py
pause
