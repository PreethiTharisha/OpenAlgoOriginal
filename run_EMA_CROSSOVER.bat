@echo off
REM OpenAlgo Instance 1 Startup Script
REM This script loads environment variables from .env.indmoney and starts Instance 1

cd /d "%~dp0"

echo Loading environment variables from .env.indmoney...
for /f "usebackq tokens=1* delims==" %%a in (.env.indmoney) do (
    if not "%%a"=="" (
        if not "%%a:~0,1%"=="#" (
            set "%%a=%%b"
        )
    )
)

REM Set the environment file path for the application
set OPENALGO_ENV_FILE=%cd%\.env.indmoney

echo.
echo Starting OpenAlgo Instance 1...
echo FLASK_PORT=%FLASK_PORT%
echo WEBSOCKET_PORT=%WEBSOCKET_PORT%
echo DATABASE_URL=%DATABASE_URL%
echo OPENALGO_ENV_FILE=%OPENALGO_ENV_FILE%
echo.

python app.py

pause
