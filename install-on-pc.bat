@echo off
title LKQ Launch automation — install
cd /d "%~dp0"
echo.
echo This installs Git, Python, ADB, the tablet screen viewer,
echo then downloads the app from GitHub and its Python packages.
echo First run can take 15–25 minutes. Leave this window open.
echo.
pause
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-on-pc.ps1"
echo.
pause
