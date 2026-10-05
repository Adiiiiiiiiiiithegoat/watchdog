@echo off
cd /d D:\projects\watchdog
:loop
"D:\projects\watchdog\.venv\Scripts\python.exe" -u bot.py >> logs\bot_stdout.log 2>&1
echo [%date% %time%] bot.py exited, restarting in 5s >> logs\bot_stdout.log
timeout /t 5 /nobreak >nul
goto loop
