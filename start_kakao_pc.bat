@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install_kakao_pc.bat first.
  pause
  exit /b 1
)
echo Keep this window open while importing schedules.
".venv\Scripts\python.exe" kakao_local_agent.py
pause
