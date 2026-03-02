@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

REM ============================================================
REM 3Dps - Starting Service
REM ============================================================

echo ========================================
echo 3Dps - Starting Service
echo ========================================
echo.

REM ── Create .runtime directory ──────────────────────────────
if not exist ".runtime" mkdir ".runtime"

echo [1/7] Checking Python...
python --version
if errorlevel 1 (
    echo [ERROR] Python not found in PATH
    echo         Install from https://python.org and enable "Add to PATH"
    goto :FAIL
)
echo.

echo [2/7] Checking virtual environment...
if exist ".venv\Scripts\activate.bat" (
    echo [INFO] Found .venv, activating...
    call .venv\Scripts\activate.bat
    if errorlevel 1 goto :FAIL
    echo [OK] Virtual environment activated
) else (
    echo [INFO] No .venv found, creating...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv
        goto :FAIL
    )
    call .venv\Scripts\activate.bat
    if errorlevel 1 goto :FAIL
    echo [OK] Virtual environment created and activated
)
echo.

echo [3/7] Checking dependencies...
python -c "import uvicorn, fastapi, PIL" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Dependencies missing, installing...
    python -m pip install --upgrade pip >nul 2>&1
    python -m pip install -r backend\requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies
        goto :FAIL
    )
    echo [OK] Dependencies installed
) else (
    echo [OK] Dependencies OK
)
echo.

echo [4/7] Checking ffmpeg...
where ffmpeg >nul 2>&1
if errorlevel 1 (
    if exist "bin\ffmpeg.exe" (
        echo [OK] ffmpeg found in bin\
    ) else (
        echo [WARNING] ffmpeg NOT found. Video slicing will not work.
        echo           Install ffmpeg and add to PATH,
        echo           or place ffmpeg.exe into %cd%\bin\
    )
) else (
    echo [OK] ffmpeg found
)
echo.

REM ── Step 5: Check if service is already running ────────────
echo [5/7] Checking if service is already running...

if not exist ".runtime\server.pid" goto :NO_PID_FILE

set /p OLD_PID=<".runtime\server.pid"
echo       Found PID file: !OLD_PID!

REM Check if process with that PID exists
tasklist /fi "PID eq !OLD_PID!" 2>nul | find "!OLD_PID!" >nul
if errorlevel 1 (
    echo       Process !OLD_PID! is dead, cleaning up
    del ".runtime\server.pid" 2>nul
    goto :NO_PID_FILE
)

REM Process alive - check if /health responds
powershell -NoProfile -Command "try { $null = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 2; exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 goto :STALE_PROCESS

REM Server is alive and responding
echo [INFO] Service is already running ^(PID !OLD_PID!^)
start "" http://127.0.0.1:8000
echo.
echo ========================================
echo   Service is already running!
echo   PID: !OLD_PID!
echo   Browser: http://127.0.0.1:8000
echo   To stop:  stop_3dps.bat
echo ========================================
goto :END

:STALE_PROCESS
echo       Process !OLD_PID! alive but /health not responding
echo       Stopping stale process...
taskkill /PID !OLD_PID! /T /F >nul 2>&1
timeout /t 2 /nobreak >nul
del ".runtime\server.pid" 2>nul

:NO_PID_FILE
REM Check if port 8000 is occupied by someone else
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue;" ^
  "if ($c) { exit 1 } else { exit 0 }"
if not errorlevel 1 (
    echo [OK] Port 8000 is free
    goto :PORT_FREE
)

REM Port occupied - show what process is using it
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue;" ^
  "if (-not $c) { exit 0 };" ^
  "$opid = $c[0].OwningProcess;" ^
  "$p = Get-Process -Id $opid -ErrorAction SilentlyContinue;" ^
  "$n = if($p){$p.ProcessName}else{'unknown'};" ^
  "Write-Host \"[ERROR] Port 8000 is already occupied by: $n (PID $opid)\";" ^
  "Write-Host '        Stop that process first, or run stop_3dps.bat'"
goto :FAIL

:PORT_FREE
echo.

REM ── Step 6: Start backend server ───────────────────────────
echo [6/7] Starting backend server...
cd /d "%~dp0backend"
start "3Dps-Server" /min python main.py --service
cd /d "%~dp0"

REM Wait for PID file to appear (max 10 seconds)
set "PID_FOUND=0"
set "PID_WAIT=0"
:PID_WAIT_LOOP
if "!PID_FOUND!"=="1" goto :PID_WAIT_DONE
set /a "PID_WAIT+=1"
if !PID_WAIT! gtr 10 goto :PID_WAIT_DONE
if exist ".runtime\server.pid" (
    set "PID_FOUND=1"
    goto :PID_WAIT_DONE
)
timeout /t 1 /nobreak >nul
goto :PID_WAIT_LOOP

:PID_WAIT_DONE
if "!PID_FOUND!"=="0" (
    echo [ERROR] Server did not create PID file within 10 seconds
    echo         Check .runtime\server.log for errors
    if exist ".runtime\server.log" (
        echo.
        echo --- Last 20 lines of server.log ---
        powershell -NoProfile -Command "Get-Content '%~dp0.runtime\server.log' -Tail 20 -ErrorAction SilentlyContinue"
    )
    goto :FAIL
)

set /p SERVER_PID=<".runtime\server.pid"
echo [OK] Server process started (PID !SERVER_PID!)
echo      Log: %~dp0.runtime\server.log
echo.

REM ── Step 7: Wait for /health endpoint ──────────────────────
echo [7/7] Waiting for server to be ready (/health)...
set "HEALTH_COUNT=0"

:HEALTH_LOOP
set /a "HEALTH_COUNT+=1"
if !HEALTH_COUNT! gtr 30 goto :HEALTH_TIMEOUT

REM Check if process is still alive
tasklist /fi "PID eq !SERVER_PID!" 2>nul | find "!SERVER_PID!" >nul
if errorlevel 1 goto :SERVER_DIED

timeout /t 1 /nobreak >nul

REM Check /health endpoint
powershell -NoProfile -Command "try { $null = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 1; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :HEALTH_OK

goto :HEALTH_LOOP

:SERVER_DIED
echo.
echo [ERROR] Server process died ^(PID !SERVER_PID!^)
echo         Check log: %~dp0.runtime\server.log
echo.
if exist ".runtime\server.log" (
    echo --- Last 20 lines of server.log ---
    powershell -NoProfile -Command "Get-Content '%~dp0.runtime\server.log' -Tail 20 -ErrorAction SilentlyContinue"
)
del ".runtime\server.pid" 2>nul
goto :FAIL

:HEALTH_TIMEOUT
echo [ERROR] Server did not respond to /health within 30 seconds
echo         PID: !SERVER_PID!
echo         Check log: %~dp0.runtime\server.log
echo.
if exist ".runtime\server.log" (
    echo --- Last 20 lines of server.log ---
    powershell -NoProfile -Command "Get-Content '%~dp0.runtime\server.log' -Tail 20 -ErrorAction SilentlyContinue"
)
goto :FAIL

:HEALTH_OK
echo [OK] Server is ready! /health returned 200
echo.
start "" http://127.0.0.1:8000
echo ========================================
echo   Service is running!
echo   PID: !SERVER_PID!
echo   Browser: http://127.0.0.1:8000
echo   Log: %~dp0.runtime\server.log
echo   To stop:  stop_3dps.bat
echo ========================================
goto :END

:END
echo.
pause
exit /b 0

:FAIL
echo.
echo [FAIL] Startup failed. Window will stay open.
if exist ".runtime\server.log" (
    echo        Log: %~dp0.runtime\server.log
    start "" notepad "%~dp0.runtime\server.log"
)
pause
exit /b 1
