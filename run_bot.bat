@echo off
rem Starts the Telegram assistant. Run setup first: see README.md.
echo Iniciando el bot de Paradise Tour...
cd /d "%~dp0backend"
if exist "..\.venv\Scripts\python.exe" (set PY=..\.venv\Scripts\python.exe) else (set PY=..\venv\Scripts\python.exe)
%PY% manage.py migrate --noinput
%PY% manage.py runbot
pause
