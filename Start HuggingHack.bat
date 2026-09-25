@echo off
setlocal
cd /d "%~dp0"
if not exist ".env" copy /Y ".env.example" ".env" >nul
echo Starting HuggingHack...
rem Build only when the image is missing: an offline server loads a prebuilt image
rem (docker load) and cannot download the packages a build needs.
docker image inspect hugginghack:local >nul 2>&1
if errorlevel 1 (
  docker compose up --build -d
) else (
  docker compose up -d
)
if errorlevel 1 (
  echo.
  echo HuggingHack could not start. Make sure Docker Desktop is running.
  pause
  exit /b 1
)
echo.
echo HuggingHack is starting at http://localhost:7860
start "" "http://localhost:7860"

