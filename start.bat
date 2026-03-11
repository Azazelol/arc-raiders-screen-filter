@echo off
cd /d "%~dp0"

:: Добавляем Python в PATH на случай если он ещё не подхватился
set "PATH=%LocalAppData%\Programs\Python\Python312\;%LocalAppData%\Programs\Python\Python312\Scripts\;%PATH%"

pip install -r requirements.txt >nul 2>&1
python filter.py
pause
