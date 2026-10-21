@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
py -3 --version >nul 2>&1
if errorlevel 1 goto use_python
py -3 -m venv .venv
if errorlevel 1 goto failed
goto install
:use_python
python --version >nul 2>&1
if errorlevel 1 goto no_python
python -m venv .venv
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -m pip install -r requirements-pc.txt
if errorlevel 1 goto failed
echo Installation complete. Double-click start_kakao_pc.bat next.
pause
exit /b 0
:no_python
echo Install Python from https://www.python.org/downloads/windows/
echo Select Add Python to PATH during installation, then run this file again.
pause
exit /b 1
:failed
echo Installation failed. Check the error above and your Internet connection.
pause
exit /b 1
