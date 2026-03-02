@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

REM ============================================================
REM 3Dps - Stopping Service
REM ============================================================

echo ========================================
echo 3Dps - Stopping Service
echo ========================================
echo.

REM ── Step 1: Stop by PID file ───────────────────────────────
echo [1/3] Checking PID file...

if not exist ".runtime\server.pid" (
    echo       No PID file found
    goto :STEP2
)

set /p SERVER_PID=<".runtime\server.pid"
echo       Found PID file: !SERVER_PID!

REM Check if process with that PID exists
tasklist /fi "PID eq !SERVER_PID!" 2>nul | find "!SERVER_PID!" >nul
if errorlevel 1 (
    echo       Process !SERVER_PID! is already dead
    del ".runtime\server.pid" 2>nul
    echo       PID file removed
    goto :STEP2
)

echo       Process is alive, stopping process tree...
taskkill /PID !SERVER_PID! /T /F >nul 2>&1
echo       Waiting for process to exit...
timeout /t 3 /nobreak >nul

REM Verify the process is gone
tasklist /fi "PID eq !SERVER_PID!" 2>nul | find "!SERVER_PID!" >nul
if not errorlevel 1 (
    echo       [WARN] Process !SERVER_PID! still alive, retrying...
    taskkill /PID !SERVER_PID! /T /F >nul 2>&1
    timeout /t 2 /nobreak >nul
)

del ".runtime\server.pid" 2>nul
echo       [OK] Process stopped, PID file removed
echo.

REM ── Step 2: Verify port 8000 is free ───────────────────────
:STEP2
echo [2/3] Checking port 8000...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue;" ^
  "if ($c) { exit 1 } else { exit 0 }"
if not errorlevel 1 (
    echo       [OK] Port 8000 is free
    goto :FINAL_CHECK
)

REM Port still busy - fallback: identify and kill the occupying process
echo       Port 8000 still occupied, attempting fallback...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue;" ^
  "if (-not $c) { Write-Host '  Port already free'; exit 0 };" ^
  "$opid = $c[0].OwningProcess;" ^
  "$proc = Get-Process -Id $opid -ErrorAction SilentlyContinue;" ^
  "$pname = if($proc){$proc.ProcessName}else{'unknown'};" ^
  "Write-Host \"  Occupying process: $pname (PID $opid)\";" ^
  "" ^
  "if ($pname -eq 'python') {" ^
  "  $wmi = Get-CimInstance Win32_Process -Filter (\"ProcessId=$opid\") -ErrorAction SilentlyContinue;" ^
  "  $cmdline = if($wmi){$wmi.CommandLine}else{''};" ^
  "  $exePath = if($wmi){$wmi.ExecutablePath}else{''};" ^
  "  Write-Host \"  Command: $cmdline\";" ^
  "  Write-Host \"  Executable: $exePath\";" ^
  "  if ($cmdline -like '*main.py*' -or $exePath -like '*3Dps*') {" ^
  "    Write-Host '  Confirmed as 3Dps server process, killing tree...';" ^
  "    $null = taskkill /PID $opid /T /F 2>&1;" ^
  "    Start-Sleep -Seconds 2;" ^
  "    $c2 = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue;" ^
  "    if ($c2) { Write-Host '  Port still busy after kill'; exit 1 }" ^
  "    else { Write-Host '  [OK] Port freed'; exit 0 }" ^
  "  } else {" ^
  "    Write-Host '  WARNING: python process does not appear to be 3Dps server';" ^
  "    Write-Host '  Command line does not contain main.py or 3Dps path';" ^
  "    Write-Host '  NOT killing. Please stop it manually.';" ^
  "    exit 2;" ^
  "  }" ^
  "} else {" ^
  "  Write-Host \"  WARNING: port 8000 occupied by non-python process ($pname)\";" ^
  "  Write-Host '  NOT killing. Please stop it manually.';" ^
  "  exit 2;" ^
  "}"

echo.

REM ── Step 3: Final verification ─────────────────────────────
:FINAL_CHECK
echo [3/3] Final verification...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue;" ^
  "if ($c) {" ^
  "  $opid = $c[0].OwningProcess;" ^
  "  $proc = Get-Process -Id $opid -ErrorAction SilentlyContinue;" ^
  "  $pname = if($proc){$proc.ProcessName}else{'unknown'};" ^
  "  Write-Host \"  [WARN] Port 8000 still busy: $pname (PID $opid)\";" ^
  "  exit 1;" ^
  "} else {" ^
  "  Write-Host '  [OK] Port 8000 is free';" ^
  "  exit 0;" ^
  "}"

set "RC=!errorlevel!"

REM Clean up PID file if it still exists
if exist ".runtime\server.pid" del ".runtime\server.pid" 2>nul

echo.
if "!RC!"=="0" (
    echo ========================================
    echo   Service stopped. Port 8000 is free.
    echo ========================================
) else (
    echo ========================================
    echo   Could not free port 8000.
    echo   Try running as admin or reboot.
    echo ========================================
)

echo.
pause
exit /b !RC!
