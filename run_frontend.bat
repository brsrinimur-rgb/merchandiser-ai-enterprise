@echo off
cd /d "%~dp0frontend"
echo Starting frontend at http://127.0.0.1:5500
"%~dp0.venv\Scripts\python.exe" -m http.server 5500 --bind 127.0.0.1
echo.
echo Frontend stopped. Review the error above.
pause
