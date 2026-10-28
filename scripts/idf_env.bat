@echo off
rem Deterministic IDF v5.5.6 environment (mirrors C:\Espressif\tools\Microsoft.v5.5.6.PowerShell_profile.ps1)
rem Usage: idf_env.bat <idf.py args...>   e.g. idf_env.bat -p COM3 flash
rem MSYSTEM MUST be undefined: idf.py main() is skipped entirely when it is set
rem (idf.py prints the MSys warning and exits 0 without building!)
set "MSYSTEM="
set "MSYSTEM_CARCH="
set "MSYSTEM_CHOST="
set "MSYSTEM_PREFIX="
set "IDF_PATH=F:\ESP\.espressif\v5.5.6\esp-idf"
set "IDF_TOOLS_PATH=C:\Espressif\tools"
set "IDF_PYTHON_ENV_PATH=C:\Espressif\tools\python\v5.5.6\venv"
set "PATH=C:\Espressif\tools\cmake\3.30.2\bin;C:\Espressif\tools\ninja\1.12.1;C:\Espressif\tools\xtensa-esp-elf\esp-14.2.0_20260121\xtensa-esp-elf\bin;C:\Espressif\tools\riscv32-esp-elf\esp-14.2.0_20260121\riscv32-esp-elf\bin;C:\Espressif\tools\ccache\4.12.1\ccache-4.12.1-windows-x86_64;C:\Espressif\tools\esp-rom-elfs\20260528\;C:\Espressif\tools\python\v5.5.6\venv\Scripts;%PATH%"
cd /d F:\ESP\projects\ESP32-CIS\ESP32_CIS_V1.0
"C:\Espressif\tools\python\v5.5.6\venv\Scripts\python.exe" "F:\ESP\.espressif\v5.5.6\esp-idf\tools\idf.py" %*
