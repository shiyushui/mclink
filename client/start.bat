@echo off
chcp 65001 >nul 2>&1
title McLink - Port Mapping Client
cd /d "%~dp0"

rem ---- locate a usable Python interpreter (no nested blocks, no for/f) ----
set "PYEXE=python"
%PYEXE% --version >nul 2>nul
if errorlevel 1 set "PYEXE=py -3"
%PYEXE% --version >nul 2>nul
if errorlevel 1 goto nopython

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo.
echo   ==========================================================
echo     McLink  -  Port Mapping Client
echo   ==========================================================
echo.
echo     Web console : http://127.0.0.1:8787
echo     Config file : %~dp0config.client.json
echo     Press Ctrl+C to stop
echo.

%PYEXE% "%~dp0mclink_client.py" -c "%~dp0config.client.json"
set RC=%errorlevel%

echo.
echo   ----------------------------------------------------------
if "%RC%"=="0" echo     Client stopped normally.
if not "%RC%"=="0" echo     Client exited with code %RC% - see the log above.
echo   ----------------------------------------------------------
echo.
pause
exit /b %RC%

:nopython
cls
echo.
echo   ==========================================================
echo     [X] Python not found
echo   ==========================================================
echo.
echo     1. Download Python 3.8+ from:
echo        https://www.python.org/downloads/
echo.
echo     2. IMPORTANT: tick "Add python.exe to PATH" in the installer.
echo.
echo     3. Close this window and run start.bat again.
echo.
pause
exit /b 1
