@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem flash_tx.bat -- build and flash a TX sender node (csi_send copies)
rem Ref: IMPLEMENTATION_PLAN.md section 10, docs/标定SOP.md section 3
rem
rem Usage:  flash_tx.bat <tx1|tx2|tx3> [COM-port] [baud]
rem e.g.    flash_tx.bat tx1 COM15
rem         flash_tx.bat tx3 COM14 460800     lower baud for unstable CH340
rem         flash_tx.bat tx1                  falls back to last-known port
rem
rem Role table (expected MACs are esptool-measured physical MACs, plan v1.1):
rem   tx1  classic-ESP32#1  project csi_tx_v1     (esp32)    expect 8C:94:DF:A1:DD:D0  last known COM15
rem   tx2  ESP32-C3         project csi_tx_c3_v1  (esp32c3)  expect E4:B0:63:33:F0:40  last known COM5
rem   tx3  classic-ESP32#2  project csi_tx_v1     (esp32)    expect 8C:94:DF:A1:DA:4C  last known COM14
rem
rem Hard rules:
rem   1) COM numbers are NOT trusted -- chip_id MAC must match or flashing is refused.
rem   2) tx3 (classic#2) once failed to enter download mode (suspected overheating):
rem      if sync fails, let the board cool down and re-plug first.
rem   3) Before multi-node deployment each TX needs a DISTINCT CSI source MAC
rem      (CONFIG_CSI_SEND_MAC, see docs/标定SOP.md 0.2); rebuild+reflash via this script.
rem ============================================================

set "ROLE=%~1"
if "%ROLE%"=="" goto usage

rem ---- role table ----
set "PROJ="
set "EXPECT_MAC="
set "DEFPORT="
if /i "%ROLE%"=="tx1" (
    set "PROJ=F:\ESP\projects\ESP32-CIS\csi_tx_v1"
    set "EXPECT_MAC=8C:94:DF:A1:DD:D0"
    set "DEFPORT=COM15"
)
if /i "%ROLE%"=="tx2" (
    set "PROJ=F:\ESP\projects\ESP32-CIS\csi_tx_c3_v1"
    set "EXPECT_MAC=E4:B0:63:33:F0:40"
    set "DEFPORT=COM5"
)
if /i "%ROLE%"=="tx3" (
    set "PROJ=F:\ESP\projects\ESP32-CIS\csi_tx_v1"
    set "EXPECT_MAC=8C:94:DF:A1:DA:4C"
    set "DEFPORT=COM14"
)
if "%PROJ%"=="" goto usage

set "PORT=%~2"
if "%PORT%"=="" set "PORT=%DEFPORT%"
set "BAUD=%~3"
if "%BAUD%"=="" set "BAUD=921600"

set "ESPTOOL=C:\Espressif\tools\python\v5.5.6\venv\Scripts\esptool.exe"
set "IDFPY=C:\Espressif\tools\python\v5.5.6\venv\Scripts\python.exe"
set "IDFPY_MAIN=F:\ESP\.espressif\v5.5.6\esp-idf\tools\idf.py"
set "CHIPID_LOG=%TEMP%\flash_tx_chipid.txt"

echo [1/3] esptool chip_id: verifying %PORT% identity (expect %ROLE% MAC %EXPECT_MAC%) ...
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
    echo [ABORT] MAC mismatch (expected %EXPECT_MAC%). This port is not %ROLE%. Refusing to flash.
    exit /b 1
)
echo [OK] MAC match, target confirmed as %ROLE%.

rem ---- source-MAC ownership guard: tx1 and tx3 share project csi_tx_v1, whose
rem      CONFIG_CSI_SEND_MAC (main\app_main.c line 53) is the CSI source MAC.
rem      tx1 requires ...:00; tx3 requires a changed value (e.g. ...:02) or the
rem      two links alias at the RX. See docs/标定SOP.md section 0.2.
if /i "%ROLE%"=="tx3" (
    findstr /c:"0x1a, 0x00, 0x00, 0x00, 0x00, 0x00}" "%PROJ%\main\app_main.c" >nul 2>&1
    if not errorlevel 1 (
        echo [ABORT] csi_tx_v1 CONFIG_CSI_SEND_MAC is still 1a:00:00:00:00:00 - same as TX1.
        echo         Edit main\app_main.c line 53, change the last byte 0x00 to 0x02, then retry.
        echo         After flashing tx3, revert that byte to 0x00 and reflash tx1.
        exit /b 1
    )
)
if /i "%ROLE%"=="tx1" (
    findstr /c:"0x1a, 0x00, 0x00, 0x00, 0x00, 0x00}" "%PROJ%\main\app_main.c" >nul 2>&1
    if errorlevel 1 (
        echo [ABORT] csi_tx_v1 CONFIG_CSI_SEND_MAC is NOT 1a:00:00:00:00:00 - it was left
        echo         changed for tx3. Revert main\app_main.c line 53 last byte to 0x00, then retry.
        exit /b 1
    )
)

rem ---- deterministic IDF v5.5.6 env (same as idf_env.bat; cd to TX project here) ----
echo [2/3] Building %PROJ% ...
set "MSYSTEM="
set "MSYSTEM_CARCH="
set "MSYSTEM_CHOST="
set "MSYSTEM_PREFIX="
set "IDF_PATH=F:\ESP\.espressif\v5.5.6\esp-idf"
set "IDF_TOOLS_PATH=C:\Espressif\tools"
set "IDF_PYTHON_ENV_PATH=C:\Espressif\tools\python\v5.5.6\venv"
set "PATH=C:\Espressif\tools\cmake\3.30.2\bin;C:\Espressif\tools\ninja\1.12.1;C:\Espressif\tools\xtensa-esp-elf\esp-14.2.0_20260121\xtensa-esp-elf\bin;C:\Espressif\tools\riscv32-esp-elf\esp-14.2.0_20260121\riscv32-esp-elf\bin;C:\Espressif\tools\ccache\4.12.1\ccache-4.12.1-windows-x86_64;C:\Espressif\tools\esp-rom-elfs\20260528\;C:\Espressif\tools\python\v5.5.6\venv\Scripts;%PATH%"
cd /d "%PROJ%"
"%IDFPY%" "%IDFPY_MAIN%" build
if errorlevel 1 (
    echo [ABORT] Build failed. Nothing was flashed.
    exit /b 1
)

echo [3/3] Flashing %PORT% @%BAUD% ...
"%IDFPY%" "%IDFPY_MAIN%" -p %PORT% -b %BAUD% flash
if errorlevel 1 (
    echo [FAIL] Flash failed. For unstable CH340 retry with: flash_tx.bat %ROLE% %PORT% 460800
    exit /b 1
)
echo [DONE] %ROLE% flashed. Open monitor to confirm: connected to AP + periodic send,
echo        and copy the CSI source MAC from the boot log for peer registration.
endlocal
exit /b 0

:usage
echo Usage: flash_tx.bat ^<tx1^|tx2^|tx3^> [COM-port] [baud]
echo   tx1  classic#1  csi_tx_v1     expect MAC 8C:94:DF:A1:DD:D0  last known COM15
echo   tx2  C3         csi_tx_c3_v1  expect MAC E4:B0:63:33:F0:40  last known COM5
echo   tx3  classic#2  csi_tx_v1     expect MAC 8C:94:DF:A1:DA:4C  last known COM14
echo e.g.:  flash_tx.bat tx1 COM15   /   flash_tx.bat tx3 COM14 460800
exit /b 1
