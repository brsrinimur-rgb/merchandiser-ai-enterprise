@echo off
setlocal
cd /d "%~dp0"
echo ==================================================
echo  Merchandiser AI 360 Enterprise - Starting
echo ==================================================

if not exist ".venv\Scripts\python.exe" (
  echo Creating Python environment...
  py -m venv .venv 2>nul || python -m venv .venv
)

echo Checking required packages...
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,pandas,sqlalchemy" 2>nul
if errorlevel 1 ".venv\Scripts\python.exe" -m pip install -r backend\requirements.txt
if errorlevel 1 (
  echo ERROR: Python packages could not be installed.
  pause
  exit /b 1
)

start "Merchandiser AI Backend" cmd /k call "%~dp0run_backend.bat"
timeout /t 8 /nobreak >nul
start "Merchandiser AI Frontend" cmd /k call "%~dp0run_frontend.bat"
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:5500
echo.
echo Frontend: http://127.0.0.1:5500
echo API Docs: http://127.0.0.1:8000/docs
echo Health:   http://127.0.0.1:8000/api/health
echo Keep both server windows open.
endlocal
