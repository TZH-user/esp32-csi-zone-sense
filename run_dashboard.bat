@echo off
title CIS 区域监控
rem ESP32 CSI 桌面监控 — 一键启动（自动扫描 + 连接 + 记录）
rem 双击本文件即可；如需手动选端口，删掉 --auto-scan --auto-start --auto-record

cd /d "%~dp0pc_dashboard"
python ui_main.py --auto-scan --auto-start --auto-record
