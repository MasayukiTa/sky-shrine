@echo off
chcp 65001 > nul 2>&1
cd /d "%~dp0"
title Sky Shrine

echo.
echo  Sky Shrine - Setup
echo  ================================
echo.

:: Check Python
python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo Install from https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python found

:: Check cloudflared
where cloudflared > nul 2>&1
if errorlevel 1 (
    echo [WARN] cloudflared not found. Tunnel will not start.
    echo Install from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
    set CF_AVAILABLE=0
) else (
    echo [OK] cloudflared found
    set CF_AVAILABLE=1
)

echo.

:: Virtual environment
if not exist "venv" (
    echo [1/3] Creating virtual environment...
    python -m venv venv
)

echo [2/3] Installing dependencies...
call venv\Scripts\activate.bat
pip install -r requirements.txt -q

echo [3/3] Starting...
echo.

echo ==============================================
echo [ AIエンジンの選択 ]
echo 1: Gemini API (制限あり・無料枠)
echo 2: NVIDIA NIM (DeepSeek V3.2 / 無制限生成モード)
echo ==============================================
set /p AI_CHOICE="番号を入力してください (1 or 2): "
if "%AI_CHOICE%"=="2" (
    set AI_PROVIDER=nvidia
    echo NVIDIA NIM モードで起動します...
) else (
    set AI_PROVIDER=gemini
    echo Gemini モードで起動します...
)
echo.

:: Start Cloudflare Tunnel in background
if "%CF_AVAILABLE%"=="1" (
    echo Starting Cloudflare Tunnel...
    start "Cloudflare Tunnel" cmd /c "cloudflared tunnel --url http://localhost:5000 2>&1"
    echo [OK] Cloudflare Tunnel started in background window
    echo.
)

echo  http://localhost:5000
echo  Press Ctrl+C to stop the server
echo.

python app.py
pause
