@echo off
title CIS 区域监控
rem ESP32 CSI 桌面监控 — 一键启动（自动扫描 + 连接 + 记录）
rem 双击本文件即可；如需手动选端口，删掉 --auto-scan --auto-start --auto-record
rem
rem CIS_SCAN_SKIP：自动扫描要跳过的串口——探测会真实打开端口并拉 DTR/RTS，
rem 非本系统的设备必须排除（COM19 = 鲁班猫调试口，2026-10-29）。串口漂移后改这里。

set "CIS_SCAN_SKIP=COM19"

cd /d "%~dp0pc_dashboard"
python ui_main.py --auto-scan --auto-start --auto-record
