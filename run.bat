@echo off
REM Run Synar ERP for testing with one double-click (Windows)
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed.
  echo Download it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

if not exist .venv (
  echo == First run: preparing environment, please wait...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt
if errorlevel 1 ( echo Failed to install requirements. Check your internet connection. & pause & exit /b 1 )

set FIRST_RUN=0
if not exist db.sqlite3 set FIRST_RUN=1
python manage.py migrate -v 0
python manage.py createcachetable
if "%FIRST_RUN%"=="1" (
  echo == Creating a demo company with sample data...
  python manage.py setup_erp --demo --admin admin --password admin12345 --tax-id 310122393500003
)

echo.
echo ======================================================
echo   Synar ERP is running:  http://127.0.0.1:8000
echo   Username: admin     Password: admin12345
echo   Close this window to stop the system.
echo ======================================================
start "" http://127.0.0.1:8000
python manage.py runserver 127.0.0.1:8000
