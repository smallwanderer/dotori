@echo off
title Dotori Dev Launcher
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    pause
    exit /b
)

python install.py --run
if %errorlevel% neq 0 (
    echo [ERROR] Failed to run launcher.
    pause
    exit /b
)
