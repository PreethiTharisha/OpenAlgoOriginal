@echo off
REM OpenAlgo Pyramid Strategy Startup Script
REM This script loads environment variables from .env.indmoneyPyramid and starts the Pyramid Strategy

cd /d "%~dp0"

echo Loading environment variables from .env.indmoneyPyramid...
for /f "usebackq tokens=1* delims==" %%a in (.env.indmoneyPyramid) do (
    if not "%%a"=="" (
        if not "%%a:~0,1%"=="#" (
            set "%%a=%%b"
        )
    )
)

REM Set the environment file path for the application
set OPENALGO_ENV_FILE=%cd%\.env.indmoneyPyramid

echo.
echo Starting OpenAlgo Pyramid Strategy...
echo FLASK_PORT=%FLASK_PORT%
echo WEBSOCKET_PORT=%WEBSOCKET_PORT%
echo DATABASE_URL=%DATABASE_URL%
echo OPENALGO_ENV_FILE=%OPENALGO_ENV_FILE%
echo.

python app.py

pause
