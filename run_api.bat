@echo off
rem Starts the API for the advisors' panel on http://localhost:8000
cd /d "%~dp0backend"
if exist "..\.venv\Scripts\python.exe" (set PY=..\.venv\Scripts\python.exe) else (set PY=..\venv\Scripts\python.exe)
%PY% manage.py migrate --noinput
%PY% manage.py runserver
