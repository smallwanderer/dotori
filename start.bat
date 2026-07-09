@echo off
title Dotori Launcher
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    pause
    exit /b
)

:menu
cls
echo ============================================================
echo   Dotori Launcher
echo ============================================================
echo   [1] Install / Setup Wizard   (first run or reconfigure)
echo   [2] Start Dotori             (use saved settings)
echo   [3] Change LLM Model
echo   [4] View Available LLM Models
echo   [5] Stop Dotori Services
echo   [6] Exit
echo ============================================================
set "choice="
set /p choice="Select an option (1-6): "

if "%choice%"=="1" goto install
if "%choice%"=="2" goto run
if "%choice%"=="3" goto change_llm
if "%choice%"=="4" goto list_models
if "%choice%"=="5" goto stop
if "%choice%"=="6" exit /b
echo.
echo [ERROR] Invalid option: %choice%
pause
goto menu

:install
python install.py
if %errorlevel% neq 0 (
    echo [ERROR] Installation failed.
)
pause
goto menu

:run
python install.py --run
if %errorlevel% neq 0 (
    echo [ERROR] Failed to start Dotori.
)
pause
goto menu

:change_llm
python install.py --change-llm
if %errorlevel% neq 0 (
    echo [ERROR] Failed to change the LLM model.
)
pause
goto menu

:list_models
python install.py --list-llm-models
pause
goto menu

:stop
docker compose -f docker-compose.dev.yml down
if %errorlevel% neq 0 (
    echo [ERROR] Failed to stop Dotori services. Is Docker Desktop running?
)
pause
goto menu
