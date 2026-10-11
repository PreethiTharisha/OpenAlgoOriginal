@echo off
REM OpenAlgo Instance 2 Startup Script
REM This script loads environment variables from .env.indmoneyNoEma and starts Instance 2

cd /d "%~dp0"

echo Loading environment variables from .env.indmoneyNoEma...
for /f "usebackq tokens=1* delims==" %%a in (.env.indmoneyNoEma) do (
    if not "%%a"=="" (
        if not "%%a:~0,1%"=="#" (
            set "%%a=%%b"
        )
    )
)

REM Set the environment file path for the application
set OPENALGO_ENV_FILE=%cd%\.env.indmoneyNoEma

echo.
echo Starting OpenAlgo Instance 2...
echo FLASK_PORT=%FLASK_PORT%
echo WEBSOCKET_PORT=%WEBSOCKET_PORT%
echo DATABASE_URL=%DATABASE_URL%
echo OPENALGO_ENV_FILE=%OPENALGO_ENV_FILE%
echo.

python app.py

pause
