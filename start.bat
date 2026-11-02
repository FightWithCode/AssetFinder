@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto :failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed
echo.
echo Open http://127.0.0.1:8000 in your browser.
echo Press Ctrl+C here to stop the server.
".venv\Scripts\python.exe" -m imagesearch serve
if errorlevel 1 goto :failed
exit /b 0
:failed
echo.
echo ImageSearch could not start. Check the error above and your Python installation.
pause
exit /b 1
