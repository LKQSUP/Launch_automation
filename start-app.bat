@echo off
title LKQ Launch X431
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" (
  echo Run install-on-pc.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
echo Starting Launch X431 app...
streamlit run main.py
pause
