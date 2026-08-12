@echo off
cd /d "%~dp0backend"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Python environment is missing. Run run_all.bat first.
  pause
  exit /b 1
)
echo Starting API at http://127.0.0.1:8000
"%~dp0.venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000 --env-file .env
echo.
echo Backend stopped. Review the error above.
pause
