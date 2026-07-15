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
echo   [6] Remove LLM Runtime
echo   [7] Show Server Status
echo   [8] Advanced Network Settings
echo   [9] Exit
echo ============================================================
set "choice="
set /p choice="Select an option (1-9): "

if "%choice%"=="1" goto install
if "%choice%"=="2" goto run
if "%choice%"=="3" goto change_llm
if "%choice%"=="4" goto list_models
if "%choice%"=="5" goto stop
if "%choice%"=="6" goto remove_llm
if "%choice%"=="7" goto status
if "%choice%"=="8" goto network_menu
if "%choice%"=="9" exit /b
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
python install.py --stop
if %errorlevel% neq 0 (
    echo [ERROR] Failed to stop Dotori services. Is Docker Desktop running?
)
pause
goto menu

:remove_llm
python install.py --remove-llm
if %errorlevel% neq 0 (
    echo [ERROR] Failed to remove the LLM runtime.
)
pause
goto menu

:status
python install.py --status
pause
goto menu

:network_menu
cls
echo ============================================================
echo   Advanced Network Settings
echo ============================================================
echo   [1] Create external access configuration files
echo   [2] Open external access configuration folder
echo   [3] Connect external access module
echo   [4] Disconnect external access module
echo   [5] Show external access status
echo   [6] Back
echo ============================================================
set "network_choice="
set /p network_choice="Select an option (1-6): "

if "%network_choice%"=="1" goto network_create
if "%network_choice%"=="2" goto network_open
if "%network_choice%"=="3" goto network_connect
if "%network_choice%"=="4" goto network_disconnect
if "%network_choice%"=="5" goto network_status
if "%network_choice%"=="6" goto menu
echo.
echo [ERROR] Invalid option: %network_choice%
pause
goto network_menu

:network_create
python install.py --network-access-create
pause
goto network_menu

:network_open
python install.py --network-access-open
if %errorlevel% neq 0 (
    echo [ERROR] Failed to open the configuration folder.
    pause
)
goto network_menu

:network_connect
python install.py --network-access-connect
if %errorlevel% neq 0 (
    echo [ERROR] External access was not connected. Review the configuration files.
)
pause
goto network_menu

:network_disconnect
python install.py --network-access-disconnect
if %errorlevel% neq 0 (
    echo [ERROR] Failed to disconnect external access.
)
pause
goto network_menu

:network_status
python install.py --network-access-status
pause
goto network_menu
