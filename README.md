# ESP32 CSI 多监测点扰动特征监控系统

微机原理课程大作业·题目3。基于 ESP32 WiFi CSI（信道状态信息）的无接触人体活动感知：**4 节点收发一体全互联（12 条感知链路）**，每块板既是发射源又是接收观测者，ESP-NOW 汇聚到 S3 统一串口上报；逐链路滑窗统计（均值/方差/峰峰值）+ 滞回 FSM 做运动/存在检测与**粗粒度区域定位**，PySide6+pyqtgraph 桌面上位机实时可视化。

> 基线保底：`v0.1-baseline`（三发一收完整版，`git checkout v0.1-baseline` 随时还原）。

- 唯一执行依据：[docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)（含 v1.1/v1.2 更新块，先读它）
- 调研报告：[docs/CSI_SURVEY.md](docs/CSI_SURVEY.md)（802.11bf 已转正、AGC 陷阱、ESPectre 升级路线）

## 系统架构

```
经典#2 TX3 ■──────┼──────■ C3 TX2
   (西北)  │  12 条链路  │  (东北)
          │  4×3 全互联  │
          │   ● 人（区域俯视图加权定位）
经典#1 TX1 ■──────┼──────● S3 RX ──串口 2M──> PC 上位机
   (东南)            (西南)
```

- 每块板 10Hz 广播 ESP-NOW 源帧（占空比 ~1%），同时开 CSI 接收监听其他板的帧
- CSI 特征按 **observer→source** 链路分桶（谁测的 → 测的谁），TX 板经 ESP-NOW 单播回传 S3
- S3 汇总 12 条链路统一上报：`0x12` 特征 / `0x13` 事件 / `0x14` 远端标定（新增），兼容遗留 `0x11`
- 上位机：12 张区域卡片、12 条 var 曲线、区域俯视图（三/四链路归一化响应加权出人位置）

## 功能亮点（上位机）

| 功能 | 说明 |
|---|---|
| 区域俯视粗定位 | 四角设备锚点 + 链路响应加权，活动时实时定位，静止保持上次位置 30s |
| 全量标定 | 10s 清场倒计时后向全部链路批量下发 train，0x14/0x30 回填阈值与噪声底 |
| 冻结 | 暂停 UI 更新（数据继续流入），配合复位按钮观察事件瞬间 |
| 阈值在线调节 | 选中本地链路卡片输入新阈值即时下发；远端链路引导走重新标定 |
| 自动/手动量程 | Y 轴量程自动缩放或手动设定 |
| CSV 记录 | 每场实验一个文件（ts_iso/observer/mac/name/type/state/score/mean/var/p2p/rssi） |

## 目录

| 目录 | 内容 |
|---|---|
| `ESP32_CIS_V1.0/` | RX/汇聚固件（esp32s3，IDF v5.5.6）：cis_wifi/peer/detect/report/mesh/console + cis_protocol.h |
| `csi_tx_v1/` | TX 固件（经典 ESP32）：广播 + CSI 接收 + 特征回传（`build_tx3`/`sdkconfig.tx3` 为 TX3 第二配置） |
| `csi_tx_c3_v1/` | TX 固件（ESP32-C3 变体） |
| `pc_dashboard/` | PC 上位机：protocol.py（纯标准库 codec）、ui_main.py（主界面）、store.py、replay.py、ui_smoke.py |
| `sim/` | 检测算法 Python 仿真（黄金模型=cis_detect.c 忠实移植 + 标定信道模型） |
| `fw_idle/` | 轻载空闲固件（无 WiFi，给等待任务的板卡降温） |
| `scripts/` | 采集/验证/烧录脚本，`backup/` 内为烧录前固件全片备份 |
| `docs/` | 计划、SOP、验收表、32 页报告初稿（LaTeX） |
| `data/` | CSV 记录（YYYYMMDD_HHMM，不入库） |

## 硬件映射（COM 号会变，**以 esptool 读 MAC 为唯一锚点**）

| 板 | 节点 id | 角色 | MAC | 固件 |
|---|---|---|---|---|
| S3#1 | 3 | RX/汇聚 | DC:DA:0C:1D:AE:10 | ESP32_CIS_V1.0 |
| 经典#1 | 0 | TX1 | 8C:94:DF:A1:DD:D0（CSI 帧 MAC=1a:...:00） | csi_tx_v1 |
| C3 | 1 | TX2 | E4:B0:63:33:F0:40（CSI 帧 MAC=1a:...:01） | csi_tx_c3_v1 |
| 经典#2 | 2 | TX3 | 8C:94:DF:A1:DA:4C（CSI 帧 MAC=1a:...:02） | csi_tx_v1（TX3 配置） |

## 快速复现

```powershell
# 1) 环境（PowerShell；从 Git Bash 启动须先 Remove-Item Env:MSYSTEM）
C:\Espressif\tools\Microsoft.v5.5.6.PowerShell_profile.ps1

# 2) 固件编译/烧录（口以 MAC 验证为准）
idf.py build && idf.py -p COM3 flash monitor     # S3（RX/汇聚）
idf.py -D SDKCONFIG=sdkconfig.tx3 build           # TX3 第二配置

# 3) 上位机（连接后先点"全量标定"，10s 倒计时内清场）
python pc_dashboard\protocol_selftest.py          # 协议自测
python pc_dashboard\ui_main.py --auto-scan --auto-start --auto-record
python pc_dashboard\ui_smoke.py --offscreen       # 无硬件冒烟
```

## 协议摘要

`AA 55 | ver(0x01) | type | len(LE) | payload | crc16-CCITT-FALSE(LE)`，CRC 覆盖 ver..payload。

| type | 名称 | payload | 频率 |
|---|---|---|---|
| 0x01 | 心跳 | uptime/peers/heap/丢帧 (11B) | 1Hz |
| 0x10 | 事件 | observer+mac+state+score (15B) | 跳变即时 + MOTION 2Hz |
| 0x11 | 特征（遗留，单 RX） | mean/var/p2p/rssi (17B) | 10Hz/peer |
| 0x12 | 特征（12 链路） | observer_id+链路特征 | 10Hz/链路 |
| 0x13 | 事件（12 链路） | observer_id+事件 | 跳变即时 |
| 0x14 | 远端标定结果 | observer_id+阈值+噪声底 | train 完成时 |
| 0x20 | 日志 / 0x30 标定（遗留） | — | — |

单位：mean/p2p=centi-amp，var/score/threshold=centi-var（raw²×100，饱和 0xFFFF）。
双端对照源：`main/cis_protocol.h` ↔ `pc_dashboard/protocol.py`。

## 网络与传输要点

- 全节点连 SSID `1003-k`；STA 信道自动跟随 AP（实测信道 4）；**换手机热点亦可**（须固定 2.4GHz）。
- ESP-NOW 广播不经过 AP，路由器仅提供信道同步锚点；无 AP 环境可手动固定信道降级运行。
- **PC 与节点网段隔离**：串口 COM3@2M 是唯一可靠上行；TCP 2323 REPL 与 UDP 3333 仅同网段可达。
- 半双工自遮挡 + 4×100Hz 碰撞 → 单链路 CSI 丢包 2~5%（FSM N=10 确认可吸收）；`link list` 监控 seq 丢失率，>10% 全网同步降源帧 50Hz。

## 里程碑状态（2026-10-29）

P1 备份+烧录 ✅ ｜ M1 数据流 ✅ ｜ M2 固件+PC ✅ ｜ M3 界面/文档 ✅（32 页报告初稿）｜ M4 多节点 ✅（基线三发一收实测）｜ **v0.2 收发一体：代码+编译 ✅，待真机 12 链路联调** ｜ M5 现场验收=人工
