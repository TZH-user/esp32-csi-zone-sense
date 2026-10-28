# ESP32 CSI 多监测点扰动特征监控系统 · 实施计划

> 课程大作业题目3。本文件是唯一执行依据：换模型/换会话后，先读本文件，再按文末"启动方式"执行。
> 版本：v1（2026-10-27 批准）。配套工作流脚本：`../scripts/cis_workflow.ts`。

## ⚠️ v1.1 更新（2026-10-27 晚，实测覆盖下文旧值）

1. **路由器已更换**：SSID=**1003-k**，密码=**th880511**。下文所有 TP-LINK_B7C4/12345678/"信道 6" 的记载全部作废。STA 模式信道自动跟随 AP，无需硬编码；仅直收模式（§3 备份方案）才需手动 `esp_wifi_set_channel`。
2. **端口映射已变**（用户重插板卡，COM 号随时会变）——以 MAC 为唯一身份锚点：

| 板 | 角色 | 2026-10-27 晚实测口 | MAC（esptool 实测） | 现固件（esptool 读 app 区实测） |
|---|---|---|---|---|
| S3#1 | RX | COM3 | DC:DA:0C:1D:AE:10 | cmsis_dap_tcp（IDF v6.0.2） |
| 经典#1 | TX1 | **COM15**（COM13 又消失，用户再度挪线；esptool 实测健康） | 8C:94:DF:A1:DD:D0 | **csi_tx_v1 已烧录并验证启动**（启动日志 Project name: csi_tx_v1，IDF v5.5.6，2026-10-27 23:55 编译；factory@0x20000——此前 0x10000 读空系读错偏移的误判，非擦空；build 声明 flash 2MB 无碍） |
| C3 | TX2 | COM5 | E4:B0:63:33:F0:40 | Arduino 系应用（IDF v4.4.4 arduino-lib-builder，2023-04-20，OTA 布局） |
| 经典#2 | TX3 | COM14 | 8C:94:DF:A1:DA:4C | digital_key_esp32（IDF v6.0.2，2026-07-31 编译）；**芯片当前进不了下载模式**（CH340 正常枚举，default/no_reset 连续 3 次同步失败，疑过热卡死）→ 待用户拔下冷却重插后先烧 fw_idle 轻载固件，M4 再换 csi_tx_v1 |
| S3#2 | 热备 | 未接 | — | 未上电 |

四板均经 esptool chip_id/read_flash 实测存活。
3. **识别规则（覆盖下文"新出现端口"类启发式）**：烧录/监控前必须先 `esptool --port <口> chip_id` 读 MAC 并与上表比对，MAC 对上才动手；COM 号本身不可信。工作流子代理已占用的口（如 TX 烧录中的 COM13）主会话不得并行探测，避免端口争用。
4. 板卡发热说明：经典#1 此前长时间运行 cmsis_dap_tcp（SoftAP+TCP+USB 桥，负载高）发热明显属正常现象，全部换烧 csi_tx_v1（STA+间歇发包）后负载显著下降；若某板过烫可在其烧录完成后拔下散热，M4 阶段工作流会 escalate 再请求上电。
5. **当前为近距台架布局（用户 2026-10-27 晚确认）**：左→右依次 经典#1 – C3 – S3#1 – 经典#2，间距 <0.5m。本阶段只验证"四板连 1003-k 互通 + CSI 数据流打通"；**M4 判据中"三链路分别遮挡无串扰"与所有 train/threshold 标定推迟到空间部署后执行**（近距下链路互相重叠、串扰是物理必然，不代表代码缺陷）；精确测量与矫正等用户规划好空间位置后按 §7 SOP 现场（或近场架高拉开）再做。经典#2 在等待任务期间由主会话烧 `fw_idle` 轻载空闲固件降温，M4 时按 MAC 重新验证后重烧 csi_tx_v1。
6. **调研结论（2026-10-28，全文 docs/CSI_SURVEY.md）**：M2 实现 cis_detect 时须**丢弃 RSSI 突变帧**（|ΔRSSI|>6dB 不进滑窗，防 AGC 增益跳变误报，esp-csi issue #185）；算法升级方向=ESPectre 式"标定期筛选高方差子载波+滑动方差"（M5 后增强项）；多目标分辨是廉价 ESP32 公认弱项，M5 按"每链路独立"管理预期；802.11bf 已于 2025-05 转正（答辩背景）。
7. **板卡硬件假设红线（用户 2026-10-28 明确）**：开发板为裸模组，**除 Flash/RAM 外不得假设任何板载外设**——S3#1 实测无三色 LED，按键/天线形式等均未确认。固件不得默认初始化 led_strip 等外设驱动（console_test 移植件若含 LED 逻辑须裁剪或确认在位再用）；状态显示全部由 PC 上位机承担（笔记本+投影），"板载状态灯"方案作废。

## ⚠️ v1.2 更新（2026-10-28 凌晨，M1 数据流验收实测）

1. **M1 验收通过（数据流部分）**：COM3@2M 录 60s（`docs/m1_sample.log`，546 帧全部 `mac=1a:00:00:00:00:00`、到达间隔 p50=110ms/max=132ms、零 >300ms 间隙、幅值 mean=37.93/std=0.099/p2p=0.64、rssi=-6 恒定、agc 恒 0）。修改前基线存 `docs/m1_sample_prefixfix_baseline.log`。**挥手扰动响应为人工项**（现场无人），留待 M2 走动验证。
2. **TX1 的 CSI 身份 = 1a:00:00:00:00:00**（上游 csi_send 示例 `esp_wifi_set_mac` 有意设置的本地管理 MAC，见 `esp-csi/examples/get-started/csi_send/main/app_main.c:47`）；esptool 物理 MAC 仍为 8C:94:DF:A1:DD:D0。`peer add` 一律用 1a:00:00:00:00:00。
3. **RX 打印修复（app_main.c v2）**：v1 的 amp 整数窗均值存在截断伪影（真值 36.5~37.5 恒打出 37，546 帧 std=0 假象）；v2 改毫单位（amp=37.93 形式）+ 窗内单包 p2p + agc_gain 诊断字段，检测逻辑未变。已重建烧录 COM3 验证（binary 850480B，00:59 烧录 Hash verified）。
4. **事故记录**：00:16/00:27 出现来源不明的并发 `idf.py flash COM3`（非 M1 验收会话所发），00:27 那次在 app 写入 17% 处被杀，分区表被擦未写回导致芯片暂时无法启动；已用本工程重新完整烧录恢复。教训：**COM3 烧录/监控期间其他会话不得并行操作该工程与串口**。
5. **新工具**：`scripts/idf_env.bat`（IDF v5.5.6 确定性环境；Git Bash 下必须经它跑 idf.py——MSYSTEM 环境变量会让 idf.py 打印 MSys 警告后**静默跳过 main() 退出 0**，经 PowerShell profile 同样中招）；`scripts/m1_capture.py`（串口采集）、`scripts/m1_analyze.py`（样本解析）。

## ⚠️ v1.3 更新（2026-10-28 凌晨，M2 固件阶段实测）

1. **M2 固件已实现并烧录 COM3**：`main/` 下 cis_protocol.h / cis_detect / cis_report / cis_wifi / cis_peer / cis_console / Kconfig.projbuild + app_main 重写（binary 0xd4dc0，Hash verified）。实测：0x01 心跳精确 1Hz（59 帧/60s、间隔恒 1000ms）、0x11 特征流 533 帧/60s（≈9Hz=esp-radar 窗口率）、0x10 即时+MOTION 期 2Hz 重发、~1200 帧零 CRC 错；REPL peer/list/scan/dump/threshold/report/udp 全通；断电重启 peer 表 NVS 恢复 + 凭据自动重连（4.9s got IP）；SoftAP 兜底实测（CIS_RX-ae10/cis12345）。日志 `docs/m2_verify_run7.log`、`run10_boot.log`、`run11_scan.log`；新工具 `scripts/m2_verify.py`（0xAA55 搜帧+CRC+重同步解析）。
2. **路由器 1003-k 实测在信道 4，不是 6**（TX1 启动日志 `connected with 1003-k, channel 4, bssid 94:e3:ee:f0:df:64`）。RX 必须 `wifi_config -s 1003-k -p th880511` 连上 AP 使信道跟随（NVS 已存，重启自动重连）；§7"路由器手动固定信道 6"在部署时仍需现场执行（或维持跟随 AP 设计）。RX 固定信道 6 收不到 TX1——M2 调试时曾因此静默无数据。
3. **网段隔离**：当前 PC 在 192.168.1.x，节点在 1003-k 的 192.168.2.x → **TCP 2323 无线 REPL 与 UDP 3333 从本 PC 不可达**，COM3@2M 串口是唯一可靠上行（协议设计以串口为主通道是正确的）；PC 上位机需接 1003-k 的 LAN 或连 RX SoftAP。固件侧 TCP 监听已确认（boot 日志 `tcp console listening on port 2323`）。
4. **实现踩坑记录**：IDF v5.5.6 的 esp_console **没有** `esp_console_new_repl_net`（计划 §4 所引 API 不存在），TCP REPL 为 cis_console.c 自实现（socket+esp_console_run+esp_log_set_vprintf 镜像）；socket() 必须在 esp_radar_init（其内部 esp_netif_init 拉起 lwip）之后调用，否则 tcpip.c:454 Invalid mbox 断言重启；esp-radar 默认 peer 不可用 wildcard（FF:FF..）——会把信道上所有邻居流量送进处理管线（实测一屏 7+ MAC）导致窗口被 "Timestamp delta out of range" 清空饿死 cb_ex，已改 filter_mac=TX1 bootstrap MAC（1a:00:00:00:00:00）；bool 型 Kconfig 关闭时宏不存在，代码须用 #ifdef。
5. **M2 判据收尾**：0x10/0x11 帧可见且数值随 RF 环境变化（var 8~1086 centi、score 多次变化、wifi_scan 信道跳扫扰动实测 0x11 空窗 2596ms 后恢复）——**真人走动扰动响应经主会话确认按台架限制推迟 M5 人工验收**（现场无人，与 v1.2#5 一致，非失败项）。train/threshold 未标定：台架共享信道噪声底 >> 默认阈值 30 → 持续 MOTION 属预期，**禁止**为"看起来安静"调高默认阈值，标定留给现场 `train`。RSSI 突变过滤（|ΔRSSI|>6dB 丢窗）已实现、实机未触发（lost 计数无增量），M5 观察确认。peer scan 在注册首个 peer 后无法发现未注册源 MAC（精确过滤设计），其余 TX MAC 按 §7 从 TX 开机日志抄取。

## 1. 目标与已确认决策
- 系统形态：N 个发送节点分布监测区 → 1 块 ESP32-S3 接收节点用 esp-radar **多 peer 按源 MAC 分发** CSI → 逐链路运动/存在检测 → 上报 PC。
- PC 端：**Python 桌面程序**（PySide6 + pyqtgraph + pyserial）：区域状态卡片、实时特征曲线、事件日志表、CSV 记录与回放。
- 板卡（已齐全，无需采购）：2×ESP32-S3、2×经典 ESP32、1×ESP32-C3。
- 执行方式：**ZCode 动态工作流，max_concurrency=2**（加主会话总并发 ≤3）。

## 2. 环境事实（2026-10-27 实测）
| 项 | 值 |
|---|---|
| IDF | v5.5.6（EIM 安装），框架 `F:\ESP\.espressif\v5.5.6\esp-idf` |
| 环境激活 | PowerShell：`C:\Espressif\tools\Microsoft.v5.5.6.PowerShell_profile.ps1` |
| IDF Python | `C:\Espressif\tools\python\v5.5.6\venv\Scripts\python.exe` |
| 已装目标 | esp32 / esp32c3 / esp32s3 |
| 官方仓库 | `F:\ESP\esp-csi`（git clone，含 examples 与 tools） |
| 已构建参考 | `F:\ESP\projects\csi_console_test`（managed_components 缓存 esp-radar 0.3.4、esp_csi_gain_ctrl、esp_radar_motion_dec、led_strip；编译链路已验证） |
| RX 工程 | `F:\ESP\projects\ESP32-CIS\ESP32_CIS_V1.0`（空模板；.vscode 已设 currentSetup 与 IDF_TARGET=esp32s3） |
| 串口 | 见顶部 v1.1 端口映射表（COM 号会变，以 MAC 为准） |
| 板内现固件 | 见顶部 v1.1 固件盘点表；**覆盖前必须 esptool 备份** |
| 路由器 | **1003-k / th880511**（v1.1 实测）；信道 STA 自动跟随 |
| S3#1 规格 | rev v0.2，16MB flash，8MB Octal PSRAM |

## 3. 节点拓扑
| 角色 | 板子 | 串口 | 说明 |
|---|---|---|---|
| RX | S3#1 | COM3（以 MAC 为准） | esp-radar 多 peer 检测 + 上报 + REPL |
| TX1 | 经典#1 8C:94:DF:A1:DD:D0 | COM15（以 MAC 为准） | csi_send 100Hz 发包，MAC=监测点标识 |
| TX2 | C3 E4:B0:63:33:F0:40 | COM5（以 MAC 为准） | 同上 |
| TX3 | 经典#2 8C:94:DF:A1:DA:4C | COM14（以 MAC 为准） | 同上 |
| 热备 | S3#2 | — | 备用 RX 或 TX4 |

组网：主=路由器模式（全节点连 **1003-k / th880511**，信道跟随 AP）；备份=`CONFIG_CIS_MODE_DIRECT` 直收模式（TX 广播 + RX `esp_wifi_set_channel` 固定同一信道混杂直收，不依赖路由器，答辩无网场地用）。

## 4. RX 固件架构（ESP32_CIS_V1.0）
移植自 `F:\ESP\esp-csi\examples\esp-radar\console_test`：`sdkconfig.defaults`、`partitions.csv`、`components/commands/`（wifi_cmd 的 NVS 凭据存储、system_cmd）。借鉴 `wifi_sensing_demo` 的 FSM 与 `esp_radar_train_start_ex` 调用序列（不搬文件）。

main/ 模块：
| 文件 | 职责 |
|---|---|
| `app_main.c` | nvs_flash_init → cis_wifi_start → esp_radar_init(注册 wifi_radar_cb_ex) → cis_peer_load(NVS 恢复+逐个 esp_radar_new_peer) → cis_detect_start → cis_report_start → cis_console_start |
| `cis_wifi.c/h` | STA 连网 + NVS 凭据 + 重连；失败 30s 开 SoftAP 兜底配网 |
| `cis_peer.c/h` | MAC→区域名表、NVS 持久化；命令 `peer add/del/list/scan` |
| `cis_detect.c/h` | **per-peer** 滑窗统计（幅值均值/方差/峰峰值）+ 滞回去抖 FSM（IDLE/MOTION/PRESENCE；连续 N≈10 帧超阈判运动、M≈25 帧低判静止起调）。自管 per-peer 上下文，**不依赖 esp_radar_motion_dec 单例** |
| `cis_report.c/h` | 二进制帧：串口主通道（COM3@2M）+ UDP 可选（默认关，端口 3333），同帧格式 |
| `cis_console.c/h` | 命令：peer/train/threshold/report/udp/dump；REPL 双开：`esp_console_new_repl_usb_serial_jtag`（COM3 双向，sdkconfig 设主 console）+ `esp_console_new_repl_net`（TCP 2323，无线标定） |
| `cis_protocol.h` | 帧定义 = PC 端 `protocol.py` 的唯一对照源 |
| `idf_component.yml` | `esp-radar: ">=0.3.4"` |
| `Kconfig.projbuild` | CIS_MODE_ROUTER/DIRECT、CIS_DEFAULT_CHANNEL=6、CIS_UDP_REPORT_DEFAULT、CIS_FEATURE_RATE_HZ=10 |

sdkconfig.defaults 要点（基于 csi_console_test 版改）：`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`（主）、`CONFIG_ESP_CONSOLE_SECONDARY_UART0=y`、`CONFIG_ESP32_WIFI_CSI_ENABLED=y`、monitor 2M、自定义分区、SPI_FLASH 16MB、Octal PSRAM 保持。

TX 固件：`csi_tx_v1/` = `examples/get-started/csi_send` 副本，仅改：目标芯片（esp32 或 esp32c3）、SSID/密码（**1003-k / th880511**）；信道跟随 AP 不硬编码（直收模式演练时再设固定信道）。

## 5. 上报协议（二进制）
```
0xAA 0x55 | ver(1B=0x01) | type(1B) | len(2B,LE) | payload | crc16-CCITT(2B,LE, 覆盖 ver..payload)
```
| type | 内容 | 频率 |
|---|---|---|
| 0x01 心跳 | uptime_ms(4B)、peer 数(1B)、free_heap(4B)、丢帧计数(2B) | 1Hz |
| 0x10 事件 | mac(6B)、name_id(1B)、state(1B:0静止/1运动/2存在)、score(2B)、rssi(1B 有符号)、ts_ms(4B) | 跳变即时 + 持续期 2Hz |
| 0x11 特征流 | mac(6B)、ts_ms(4B)、mean(2B)、var(2B)、p2p(2B)、rssi(1B) | 每 peer 10Hz（命令可调 1~50） |
| 0x20 日志回显 | ASCII | 按需 |
| 0x30 标定结果 | mac(6B)、训练时长(2B)、阈值(2B)、噪声底(2B) | train 结束 |

PC 解析：0xAA55 搜帧 + CRC 校验 + 失步重同步，容忍控制台日志字节交织。带宽 ≈1KB/s，2M 余量充足。

## 6. PC 端 pc_dashboard/
| 文件 | 职责 |
|---|---|
| `protocol.py` | 帧 codec + crc16 + dataclass（**纯标准库**） |
| `protocol_selftest.py` | 编解码往返/坏帧/重同步自测（**纯标准库，作工作流确定性门控**） |
| `serial_source.py` | COM3@2M 独立读线程 + ≥1MB 环形缓冲 + 丢帧计数；接口 start/stop/frame_queue |
| `udp_source.py` | 同接口 UDP 监听（3333） |
| `store.py` | CSV `data/YYYYMMDD_HHMM/cis_log.csv`（ts_iso,mac,name,type,state,score,mean,var,p2p,rssi）+ 回放缓冲 |
| `ui_main.py` | 顶栏（连接源/记录/帧率丢帧）；左列区域卡片（大状态灯+score+最近事件）；中央 pyqtgraph 每 peer 曲线+事件竖线；底部事件表+命令框；标定按钮 15s 倒计时自动发 `train` 并等 0x30 回填 |
| `replay.py` | CSV 按时间戳倍速回放 |
| `minimal_monitor.py` | M2 验证：打印事件行 |
| `requirements.txt` | pyserial, PySide6, pyqtgraph, numpy, pandas |

## 7. 标定与部署 SOP
1. 路由器：2.4G 信道手动固定 **6**、20MHz、关自动信道/频段合一。
2. 摆位：天线竖直同极化；离地 ≥1m、离墙/金属 ≥0.5m；链路中点覆盖活动区；胶带标记 + 拍照存档；**标定后挪动即重 train**。
3. 每房间流程（COM3 REPL 或 TCP 2323）：
   ```
   wifi_config -s 1003-k -p th880511        # 首次，之后 NVS 自动重连
   peer scan                                 # 或从 TX 开机日志抄 MAC
   peer add <tx_mac> <区域名>
   # 清场、关门、无人静止：
   train <tx_mac> 15                         # 回 0x30 帧：阈值+噪声底
   threshold <tx_mac> <val>                  # 视验收 ±10~20% 微调
   threshold save                            # NVS 持久化
   ```
4. 换场地：每链路约 30s（peer add + train 15）；无路由场地切直收模式。

## 8. 里程碑与判据
| 里程碑 | 内容 | 通过判据 | 预计 |
|---|---|---|---|
| M1 | 备份 DAPLink → TX1 烧 csi_send → RX 最小工程（单 peer 回调打印幅值） | COM3 持续滚动含 TX MAC 的 CSI 幅值行；环境扰动可见波动、静止平稳（**人走动验证=人工项**） | 1~2d |
| M2 | cis_detect/cis_report/cis_protocol + protocol.py/minimal_monitor.py | 走动触发 EVENT MOTION；静置 60s 无误报；停走 3s 内 EVENT STILL；坏帧可重同步；protocol_selftest 全绿 | 2~3d |
| M3 | ui_main/store/replay 全套 | UI 冒烟 60s 自动退退出码 0；曲线与事件一致；CSV 列齐；回放重现（30min 长稳=人工项） | 3~4d |
| M4 | TX2(C3)/TX3(经典#2) 烧录；3 peer 注册 + 分 peer train + 断电 NVS 恢复 | 三链路分别遮挡对应卡片独立变化无串扰；断电重启 peer 表/阈值仍在、30s 内恢复检测 | 2~3d |
| M5 | 部署验收 + 答辩准备 | 7 条验收用例（见下）全过（隔墙/微动允许注明降级）；备用场地 15min 重标定排练成功；直收模式演练成功 | 2d |

M5 验收用例：① 静置 30min 误报 ≤2 次/10min；② 0.5m/s 横穿 10 次 ≥9 次 2s 内报 MOTION；③ 1~2m 挥手 3s 内检出（否则记灵敏度极限）；④ 隔墙标定后走动可检（存在检测做不到注明降级）；⑤ 双区并发事件 MAC 正确无串扰；⑥ 10min 丢帧 <1%；⑦ RX 断电重启 30s 内恢复。

## 9. 风险与对策
1. COM3 原 secondary console 不能输入 → 主 console 改 USB-Serial-JTAG + TCP 2323 双 REPL。
2. 覆盖 DAPLink → 烧录前 `esptool.py read_flash` 备份至 `scripts/backup/`（cmsis_dap_tcp 开源可刷回）。
3. TX 供电抖动 → 短粗 USB 线/独立 5V；monitor 观察发包失败计数。
4. 信道 6 拥塞 → 按静置噪声底切 1/11（路由+固件同步）；终极切直收模式。
5. 2M 丢帧 → 环缓冲+丢帧计数上界面；必要时特征流降 5Hz。
6. esp_radar_motion_dec 疑似单例 → cis_detect 自管 per-peer 上下文隔离。
7. 答辩换场地 → SOP 30s/链路 + 直收模式 + 提前排练。
8. 工期 → M1+M2 为及格线；M3 可降级 Web Serial 单页（参考 wifi_sensing_demo/tools/web_serial_monitor.html）。

## 10. 目录规划
```
F:\ESP\projects\ESP32-CIS\
├── ESP32_CIS_V1.0\          # RX 工程（含 components/commands 移植）
├── csi_tx_v1\               # TX 工程（csi_send 副本改配置）
├── pc_dashboard\            # Python 桌面监控
├── scripts\                 # flash_rx.bat / flash_tx.bat / backup_flash.sh / backup/
├── data\                    # CSV 日志（YYYYMMDD_HHMM 子目录）
└── docs\                    # 本计划、标定 SOP、验收记录表、答辩演示脚本
```
常用命令：
- RX：`idf.py set-target esp32s3 && idf.py build && idf.py -p COM3 flash monitor`
- TX：先 `esptool --port <口> chip_id` 验 MAC 再 `idf.py -p <口> flash monitor`（CH340 口不稳加 `-b 460800`）
- PC：`python pc_dashboard\minimal_monitor.py --port COM3 --baud 2000000`

## 11. 工作流执行方式（换模型后照做）
1. 新会话先读本文件 + `../scripts/cis_workflow.ts`。
2. 加载技能 `dynamic-workflows`，然后：
   `CreateWorkflow(path="F:\\ESP\\projects\\ESP32-CIS\\scripts\\cis_workflow.ts", name="ESP32 CSI 监控系统实施", max_concurrency=2)`
3. 工作流 6 阶段：P1 备份+TX/RX 烧录（2 并发）→ P2 M1 数据流验收 → P3 M2 固件∥PC + 集成修复环（门控=protocol_selftest）→ P4 M3 UI∥文档 + 冒烟门 → P5 M4 多节点（缺板 escalate 请求插板）→ P6 汇总报告。
4. 人机边界：构建/烧录/代码/解析由工作流完成；走动测试、清场标定、验收用例由人按 §7/§8 执行，或工作流 escalate 时实时配合。
5. 若脚本编译诊断：按技能 §13 就地 Edit 该文件后以 `path` 重提，勿整段重贴。
