@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem flash_rx.bat -- build and flash the RX node (ESP32_CIS_V1.0, esp32s3)
rem Ref: IMPLEMENTATION_PLAN.md section 10, docs/标定SOP.md section 3
rem
rem Usage:  flash_rx.bat [COM-port] [monitor]
rem e.g.    flash_rx.bat                == flash_rx.bat COM3
rem         flash_rx.bat COM3 monitor   enter monitor after flashing
rem
rem Hard rules (IMPLEMENTATION_PLAN v1.1 #3, v1.2 #4):
rem   1) COM numbers are NOT trusted -- esptool chip_id must confirm the
rem      expected MAC (RX S3#1 = DC:DA:0C:1D:AE:10) or flashing is refused.
rem   2) While this runs, no other session may flash/monitor COM3 or touch
rem      the ESP32_CIS_V1.0 project (a concurrent flash once wiped the
rem      partition table, plan v1.2 #4).
rem ============================================================

set "PORT=%~1"
if "%PORT%"=="" set "PORT=COM3"
set "EXPECT_MAC=DC:DA:0C:1D:AE:10"
set "ESPTOOL=C:\Espressif\tools\python\v5.5.6\venv\Scripts\esptool.exe"
set "CHIPID_LOG=%TEMP%\flash_rx_chipid.txt"

echo [1/3] esptool chip_id: verifying %PORT% identity (expect RX MAC %EXPECT_MAC%) ...
"%ESPTOOL%" --port %PORT% chip_id > "%CHIPID_LOG%" 2>&1
if errorlevel 1 (
    type "%CHIPID_LOG%"
    echo [ABORT] chip_id failed: port missing/busy or board not ready.
    echo         Do NOT guess another port; locate the board first, see SOP section 0.1.
    exit /b 1
)
findstr /i "MAC:" "%CHIPID_LOG%"
findstr /i "%EXPECT_MAC%" "%CHIPID_LOG%" >nul
if errorlevel 1 (
    echo [ABORT] MAC mismatch (expected %EXPECT_MAC%). This port is not RX S3#1. Refusing to flash.
    exit /b 1
)
echo [OK] MAC match, target confirmed as RX S3#1.

echo [2/3] Building (deterministic IDF v5.5.6 env via idf_env.bat) ...
call "%~dp0idf_env.bat" build
if errorlevel 1 (
    echo [ABORT] Build failed. Nothing was flashed.
    exit /b 1
)

echo [3/3] Flashing %PORT% ...
if /i "%~2"=="monitor" (
    call "%~dp0idf_env.bat" -p %PORT% flash monitor
) else (
    call "%~dp0idf_env.bat" -p %PORT% flash
)
if errorlevel 1 (
    echo [FAIL] Flash failed. Check board/serial, then retry: re-run chip_id MAC gate first.
    exit /b 1
)
echo [DONE] RX flashed. Boot check: got ip -^> "tcp console listening on port 2323" -^> 1Hz heartbeat.
endlocal
exit /b 0
