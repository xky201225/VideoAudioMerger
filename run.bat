@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 未检测到 Python，请先安装 Python 3.9 或更高版本。
  pause
  exit /b 1
)
python app.py
pause
