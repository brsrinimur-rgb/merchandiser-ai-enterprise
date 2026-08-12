@echo off
cd /d "%~dp0"
echo Checking Merchandiser AI services...
".venv\Scripts\python.exe" -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=10)))"
if errorlevel 1 (
  echo FAILED: Backend is not reachable on port 8000.
  pause
  exit /b 1
)
start "" http://127.0.0.1:8000/docs
echo SUCCESS: Backend is healthy. Swagger is opening.
pause
