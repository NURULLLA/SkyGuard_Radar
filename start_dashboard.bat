@echo off
echo Launching Skyguard Dashboard...
start start_bot.vbs
timeout /t 3 /nobreak > nul
start http://localhost:5050
exit
