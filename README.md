# ESP32 CSI 多监测点扰动特征监控系统

微机原理课程大作业·题目3。1×ESP32-S3 接收节点用 esp-radar **多 peer 按源 MAC 分发** CSI，逐链路滑窗统计 + 滞回 FSM 做运动/存在检测，二进制协议上报 PC 桌面上位机（PySide6+pyqtgraph）。

- 唯一执行依据：[docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)（含 v1.1/v1.2 更新块，先读它）
- 调研报告：[docs/CSI_SURVEY.md](docs/CSI_SURVEY.md)（802.11bf 已转正、AGC 陷阱、ESPectre 升级路线）

## 目录

| 目录 | 内容 |
|---|---|
| `ESP32_CIS_V1.0/` | RX 固件（esp32s3，IDF v5.5.6）：cis_wifi/peer/detect/report/console + cis_protocol.h |
| `csi_tx_v1/` | TX 固件（经典 ESP32，csi_send 副本：连 1003-k、CSI 标识 MAC `1a:00:00:00:00:00`） |
| `csi_tx_c3_v1/` | TX 固件（ESP32-C3 变体） |
| `pc_dashboard/` | PC 上位机：protocol.py（纯标准库 codec）、serial_source.py、minimal_monitor.py、protocol_selftest.py（M3 增 ui_main/store/replay） |
| `sim/` | 检测算法 Python 仿真（黄金模型=cis_detect.c 忠实移植 + 标定信道模型）；`python sim/run_scenarios.py` 出六场景扫描，分析见 docs/算法仿真分析.md |
| `fw_idle/` | 轻载空闲固件（无 WiFi，给等待任务的板卡降温） |
| `scripts/` | 采集/验证/烧录脚本，`backup/` 内为烧录前固件全片备份 |
| `docs/` | 计划、SOP、验收表、日志样例 |
| `data/` | CSV 记录（YYYYMMDD_HHMM） |

## 硬件映射（COM 号会变，**以 esptool 读 MAC 为唯一锚点**）

| 板 | 角色 | MAC | 现固件 |
|---|---|---|---|
| S3#1 | RX | DC:DA:0C:1D:AE:10 | ESP32_CIS_V1.0（自研） |
| 经典#1 | TX1 | 8C:94:DF:A1:DD:D0（CSI 帧 MAC=1a:00:00:00:00:00） | csi_tx_v1 |
| C3 | TX2 | E4:B0:63:33:F0:40 | 待烧 csi_tx_c3_v1（M4） |
| 经典#2 | TX3 | 8C:94:DF:A1:DA:4C | digital_key_esp32（过热待冷却，回场后烧 fw_idle 再转任务） |

## 快速复现

```powershell
# 1) 环境（PowerShell；从 Git Bash 启动须先 Remove-Item Env:MSYSTEM）
C:\Espressif\tools\Microsoft.v5.5.6.PowerShell_profile.ps1

# 2) 烧录（口以 MAC 验证为准）
idf.py -p COM3 flash monitor    # RX（S3）
idf.py -p COM15 flash monitor   # TX1（经典#1）

# 3) 上位机
python pc_dashboard\protocol_selftest.py                    # 协议自测（20/20）
python pc_dashboard\minimal_monitor.py --port COM3 --baud 2000000
```

## 协议摘要

`AA 55 | ver(0x01) | type | len(LE) | payload | crc16-CCITT-FALSE(LE)`，CRC 覆盖 ver..payload。
类型：0x01 心跳(11B) / 0x10 事件(15B，跳变即时+MOTION 2Hz) / 0x11 特征流(17B，10Hz/peer) / 0x20 日志 / 0x30 标定结果(12B)。
单位：mean/p2p=centi-amp，var/score/threshold=centi-var（raw²×100，饱和 0xFFFF）。
双端对照源：`main/cis_protocol.h` ↔ `pc_dashboard/protocol.py`（2026-10-28 逐字段比对一致）。

## 网络与传输要点

- 全节点连 SSID `1003-k`（凭据见计划 v1.1）；STA 信道自动跟随 AP（实测信道 4）。
- **PC 与节点网段隔离**（PC 192.168.1.x / 节点 192.168.2.x）：串口 COM3@2M 是唯一可靠上行；TCP 2323 REPL 与 UDP 3333 仅同网段设备可达。
- 台架未标定阶段 var 噪声底大、默认阈值会持续 MOTION 属预期；标定按 `docs/标定SOP.md`（M3 产出）现场做。

## 里程碑状态（2026-10-28 02:30）

P1 备份+双端烧录 ✅ ｜ M1 数据流 ✅（扰动对照=人工项）｜ M2 固件 ✅（自测+烧录+零 CRC 错）·PC ✅（20/20 自测）·集成验证进行中 ｜ M3 界面/文档 ⏳ ｜ M4 多节点 ⏳ ｜ M5 现场验收=人工
