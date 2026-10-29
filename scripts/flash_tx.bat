@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem flash_tx.bat -- build and flash a TX sender node (csi_send copies)
rem Ref: IMPLEMENTATION_PLAN.md section 10, docs/标定SOP.md sections 0.2/3
rem
rem Usage:  flash_tx.bat <tx1|tx2|tx3> [COM-port] [baud]
rem e.g.    flash_tx.bat tx1 COM15
rem         flash_tx.bat tx3 COM14 460800     lower baud for unstable CH340
rem         flash_tx.bat tx1                  falls back to last-known port
rem
rem Role table (expected MACs are esptool-measured physical MACs, plan v1.1;
rem CSI source MACs derive from the per-role node config, SOP section 0.2):
rem   tx1  classic-ESP32#1  project csi_tx_v1     (esp32)    expect 8C:94:DF:A1:DD:D0  node1  source 1a:00:00:00:00:00  last known COM15
rem   tx2  ESP32-C3         project csi_tx_c3_v1  (esp32c3)  expect E4:B0:63:33:F0:40  node2  source 1a:00:00:00:00:01  last known COM5
rem   tx3  classic-ESP32#2  project csi_tx_v1     (esp32)    expect 8C:94:DF:A1:DA:4C  node3  source 1a:00:00:00:00:02  last known COM14
rem
rem Hard rules:
rem   1) COM numbers are NOT trusted -- chip_id MAC must match or flashing is refused.
rem   2) tx3 (classic#2) once failed to enter download mode (suspected overheating):
rem      if sync fails, let the board cool down and re-plug first.
rem   3) Node identity is a BUILD CONFIG choice (Kconfig CIS_TX_NODE_ID), no source
rem      edits: tx3 builds with sdkconfig.defaults.tx3 (CONFIG_CIS_TX_NODE_3=y) into
rem      build_tx3; tx1 keeps the default build. The script verifies the freshly
rem      built artifact carries the expected node identity before flashing.
rem ============================================================

set "ROLE=%~1"
if "%ROLE%"=="" goto usage

rem ---- role table ----
set "PROJ="
set "EXPECT_MAC="
set "DEFPORT="
set "BUILD_ARGS="
set "BUILD_DIR=build"
set "EXPECT_NODE="
if /i "%ROLE%"=="tx1" (
    set "PROJ=F:\ESP\projects\ESP32-CIS\csi_tx_v1"
    set "EXPECT_MAC=8C:94:DF:A1:DD:D0"
    set "DEFPORT=COM15"
    set "EXPECT_NODE=CONFIG_CIS_TX_NODE_1"
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
    set "BUILD_DIR=build_tx3"
    set "BUILD_ARGS=-B build_tx3 -D SDKCONFIG=sdkconfig.tx3 -D SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.defaults.tx3"
    set "EXPECT_NODE=CONFIG_CIS_TX_NODE_3"
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

echo [1/4] esptool chip_id: verifying %PORT% identity (expect %ROLE% MAC %EXPECT_MAC%) ...
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
    echo [ABORT] MAC mismatch - expected %EXPECT_MAC%. This port is not %ROLE%. Refusing to flash.
    exit /b 1
)
echo [OK] MAC match, target confirmed as %ROLE%.

rem ---- deterministic IDF v5.5.6 env (same as idf_env.bat; cd to TX project here) ----
echo [2/4] Building %PROJ% (role config: %BUILD_ARGS%) ...
set "MSYSTEM="
set "MSYSTEM_CARCH="
set "MSYSTEM_CHOST="
set "MSYSTEM_PREFIX="
set "IDF_PATH=F:\ESP\.espressif\v5.5.6\esp-idf"
set "IDF_TOOLS_PATH=C:\Espressif\tools"
set "IDF_PYTHON_ENV_PATH=C:\Espressif\tools\python\v5.5.6\venv"
set "PATH=C:\Espressif\tools\cmake\3.30.2\bin;C:\Espressif\tools\ninja\1.12.1;C:\Espressif\tools\xtensa-esp-elf\esp-14.2.0_20260121\xtensa-esp-elf\bin;C:\Espressif\tools\riscv32-esp-elf\esp-14.2.0_20260121\riscv32-esp-elf\bin;C:\Espressif\tools\ccache\4.12.1\ccache-4.12.1-windows-x86_64;C:\Espressif\tools\esp-rom-elfs\20260528\;C:\Espressif\tools\python\v5.5.6\venv\Scripts;%PATH%"
cd /d "%PROJ%"
"%IDFPY%" "%IDFPY_MAIN%" %BUILD_ARGS% build
if errorlevel 1 (
    echo [ABORT] Build failed. Nothing was flashed.
    exit /b 1
)

rem ---- node-identity guard on the BUILD ARTIFACT (SOP section 0.2):
rem      the generated sdkconfig.h must carry the role's CIS_TX_NODE choice.
rem      tx1/tx3 share this project; a wrong/stale build dir would alias the two
rem      links at the RX, so the check runs on every flash, not just tx3.
if not "%EXPECT_NODE%"=="" (
    echo [3/4] Verifying node identity in %BUILD_DIR%\config\sdkconfig.h ...
    findstr /c:"#define %EXPECT_NODE% 1" "%PROJ%\%BUILD_DIR%\config\sdkconfig.h" >nul 2>&1
    if errorlevel 1 (
        echo [ABORT] Build artifact does NOT carry %EXPECT_NODE% - refusing to flash.
        echo         tx1 must be built with CIS_TX_NODE_1 - default sdkconfig,
        echo         tx3 with CIS_TX_NODE_3 - sdkconfig.defaults.tx3 + build_tx3.
        echo         Delete %BUILD_DIR% and sdkconfig.tx3 if present, then retry.
        exit /b 1
    )
    echo [OK] Build artifact confirmed as %EXPECT_NODE%.
) else (
    echo [3/4] Node identity check skipped - no CIS_TX_NODE config in this project.
)

echo [4/4] Flashing %PORT% @%BAUD% ...
"%IDFPY%" "%IDFPY_MAIN%" %BUILD_ARGS% -p %PORT% -b %BAUD% flash
if errorlevel 1 (
    echo [FAIL] Flash failed. For unstable CH340 retry with: flash_tx.bat %ROLE% %PORT% 460800
    exit /b 1
)
echo [DONE] %ROLE% flashed. Open monitor to confirm: connected to AP + periodic send,
echo        boot log prints "node_id:" (tx1=1 source 1a:00:00:00:00:00, tx3=3 source
echo        1a:00:00:00:00:02) and the CSI whitelist -- copy the source MAC for
echo        peer registration.
endlocal
exit /b 0

:usage
echo Usage: flash_tx.bat ^<tx1^|tx2^|tx3^> [COM-port] [baud]
echo   tx1  classic#1  csi_tx_v1     expect MAC 8C:94:DF:A1:DD:D0  node1 source 1a:00:00:00:00:00  last known COM15
echo   tx2  C3         csi_tx_c3_v1  expect MAC E4:B0:63:33:F0:40  node2 source 1a:00:00:00:00:01  last known COM5
echo   tx3  classic#2  csi_tx_v1     expect MAC 8C:94:DF:A1:DA:4C  node3 source 1a:00:00:00:00:02  last known COM14
echo Node identity is chosen by build config (Kconfig CIS_TX_NODE_ID): tx3 builds
echo from sdkconfig.defaults.tx3 into build_tx3 -- no source edits, no MAC revert.
echo e.g.:  flash_tx.bat tx1 COM15   /   flash_tx.bat tx3 COM14 460800
exit /b 1
