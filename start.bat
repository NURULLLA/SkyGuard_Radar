@echo off
title Skyguard Flight Tracker
color 0B
echo.
echo  ================================================
echo    SKYGUARD FLIGHT TRACKER - UK-75057 / UK-75058
echo  ================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python не найден! Установите Python 3.8+
    pause
    exit /b 1
)

:: Install dependencies
echo [1/2] Установка зависимостей...
pip install -r requirements.txt -q

echo [2/2] Запуск сервера...
echo.
echo  Откройте браузер: http://localhost:5050
echo  Нажмите Ctrl+C для остановки
echo.
python app.py
pause
