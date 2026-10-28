# 标定与部署 SOP（实施计划 §7 可执行展开版）

> 适用系统：ESP32 CSI 多监测点扰动特征监控系统（1×RX + 3×TX + 路由器 1003-k）。
> 本文是 `docs/IMPLEMENTATION_PLAN.md` §7 的逐步操作展开，命令均以当前固件实际注册的 REPL 命令为准（`ESP32_CIS_V1.0/main/cis_console.c:451-474`）。
> 配套：验收记录 `docs/验收记录表.md`；烧录脚本 `scripts/flash_rx.bat`、`scripts/flash_tx.bat`；备份脚本 `scripts/backup_flash.sh`。
> 版本：v1.1（2026-10-29，§0.2 节点身份改为 Kconfig 构建配置派生、废除改源码换 MAC 流程，新增 §0.3 跨 observer 刻度差异；§3/§9 同步）。v1（2026-10-28，依据实施计划 v1.3 状态编写）。

---

## 0. 前置条件（每次现场先过一遍）

| 项 | 要求 | 检查方法 |
|---|---|---|
| 路由器 | SSID=`1003-k`，密码=`th880511`，2.4G | 手机可连即通 |
| 烧录环境 | IDF v5.5.6 确定性环境 | `scripts/idf_env.bat --version` 打印 ESP-IDF 版本不报错 |
| esptool | IDF venv 内置 | `C:\Espressif\tools\python\v5.5.6\venv\Scripts\esptool.exe version`（实测 v4.12.0） |
| 串口独占 | 任一时刻只允许一个会话操作一个 COM 口 | 烧录/监控 COM3 期间其他会话禁止并行操作该工程与串口（v1.2 #4 事故教训：并发 flash 曾把分区表擦掉） |
| PC 上位机 | `pc_dashboard/` 依赖已装 | `python pc_dashboard\minimal_monitor.py --port COM3 --baud 2000000` 能出帧 |

### 0.1 身份锚定规则（红线，COM 号不可信）

用户重插板卡后 COM 号随时会变，**任何烧录/监控前必须先验 MAC**：

```
C:\Espressif\tools\python\v5.5.6\venv\Scripts\esptool.exe --port <口> chip_id
```

输出 `MAC:` 行与下表比对，对上才动手。板卡物理 MAC 表（v1.1 实测，作为期望值写死在脚本里）：

| 板 | 角色 | 物理 MAC（esptool 实测） | 最近已知 COM | Flash 容量 |
|---|---|---|---|---|
| S3#1 | RX | `DC:DA:0C:1D:AE:10` | COM3 | 16M |
| 经典#1 | TX1 | `8C:94:DF:A1:DD:D0` | COM15 | 4M |
| C3 | TX2 | `E4:B0:63:33:F0:40` | COM5 | 4M |
| 经典#2 | TX3 | `8C:94:DF:A1:DA:4C` | COM14（v1.1 时曾进不了下载模式，疑过热；凉后重试） | 4M |

### 0.2 CSI 源 MAC ≠ 物理 MAC（节点身份 ≡ 源 MAC 末字节）

- 每个 TX 固件内 `esp_wifi_set_mac` 有意设置了本地管理地址作为 **CSI 源 MAC**（原 `CONFIG_CSI_SEND_MAC`，现由构建配置派生）。**mesh 节点身份（observer_id）≡ 源 MAC 末字节**，与 PC 上位机 `MAC_DEVICE_MAP`（`ui_main.py`，按末字节映射）天然对齐。四节点身份表（v1.4 mesh 化后）：

  | observer_id | 节点 | 工程 / 构建配置 | CSI 源 MAC |
  |---|---|---|---|
  | 0x00 | 经典#1 TX1（node1） | `csi_tx_v1` 默认构建（Kconfig `CIS_TX_NODE_1`） | `1a:00:00:00:00:00` |
  | 0x01 | C3 TX2（node2） | `csi_tx_c3_v1` | `1a:00:00:00:00:01` |
  | 0x02 | 经典#2 TX3（node3） | `csi_tx_v1` + `sdkconfig.defaults.tx3`（Kconfig `CIS_TX_NODE_3`） | `1a:00:00:00:00:02` |
  | 0x03 | S3 RX（node4） | `ESP32_CIS_V1.0`，烧录前 `esp_wifi_set_mac` 自定义 STA MAC | `1a:00:00:00:00:03` |

- **源 MAC 由构建配置决定**（`csi_tx_v1/main/Kconfig.projbuild` 的 `CIS_TX_NODE_ID` choice：node1→…:00 / node3→…:02）。`flash_tx.bat tx3` 自动以 `sdkconfig.defaults.tx3`（`CONFIG_CIS_TX_NODE_3=y`）+ `build_tx3` 独立构建，`flash_tx.bat tx1` 用默认 `build`——**同一工程两套构建产物互不污染：换角色不需要改源码，也没有"改 MAC-烧 TX3-改回-重烧 TX1"流程（v1 版流程已废除）**。脚本防呆升级为校验**构建产物** `build*/config/sdkconfig.h` 里的 node 选择，与角色不符即拒烧。
- ⚠️ **四节点源 MAC 必须互不相同**，否则 RX 侧无法按源 MAC 区分链路、多 peer 全部混叠。烧错角色（如 tx1 烧成 node3 配置）时，该板开机 monitor 日志的 `node_id:` 行与 `mac=` 会立即暴露错配。
- RX 精确过滤设计：注册第一个 peer 后 `peer scan` **只能看到已注册源**；未注册 TX 的源 MAC 从该 TX 开机 monitor 日志的 `mac=` / `node_id:` 字段抄取（v1.3 #5 实测结论）。

### 0.3 跨 observer 幅值刻度差异（只比相对量）

TX 节点（`csi_tx_v1/main/cis_csi_rx.c`）用**原始 `wifi_csi_info_t` 全 tone I/Q** 计算特征；S3 用 **esp-radar 重建的 L-LTF 子载波集**（`esp_radar.c` `esp_radar_rebuild_csi_data`，仅取 L-LTF 有效子载波段）。同一物理链路在两侧 observer 的 mean/var **绝对刻度不同**：

- 跨 observer 只比较**相对量**（var 相对变化、超阈倍数），不直接比较绝对值；
- 每条 (observer, source) 链路**独立 train 归一**（TX 侧 `cis_tx_train` per-source 标定 + 0x14 标定帧即为此设计），阈值/噪声底只在同 observer 同链路内比较，不可跨 observer 复用。

---

## 1. 路由器设置（§7.1，现场执行）

1. 登录路由器管理页（1003-k 的网关，通常 `192.168.2.1`）。
2. 无线设置（2.4G）：
   - **信道**：手动固定 **6**（实测当前 AP 在信道 4，见 v1.3 #2；改 6 后全节点 STA 跟随，无需改固件；若现场改不了，可接受跟随 AP——但必须全系统一致，见 §5 排错第 1 条）；
   - **带宽**：20MHz（关 40MHz/自动）；
   - **自动信道**：关闭；**频段合一（2.4G/5G 同名）**：关闭；
   - 记下实际信道写入验收记录表头部。
3. 把 PC 上位机接入 1003-k 的 LAN（节点在 192.168.2.x；PC 若留在别的网段，TCP 2323 REPL 与 UDP 3333 均不可达，v1.3 #3），或直接连 RX 的 SoftAP 兜底 `CIS_RX-ae10` / `cis12345` 后从 192.168.4.x 访问。

## 2. 摆位（§7.2，现场执行）

1. 每个监测点 1 块 TX + 1 条对应链路：TX 摆在活动区一侧，RX 在另一侧，**链路中点覆盖活动区**。
2. 姿态：天线**竖直、同极化**（TX 与 RX 天线方向一致）。
3. 高度与净空：离地 **≥1 m**；离墙、金属柜、显示器等大反射体 **≥0.5 m**。
4. 固定：胶带在桌面/墙面标记每块板的位置轮廓，并**拍照存档**（照片按 `data/<日期>_部署照/` 存）。
5. 标红线：**标定后挪动任何一块板 = 该链路作废，必须重 train**（物理位置变了噪声底就变）。
6. 近距台架（间距 <0.5m）只用于功能联调，不用于标定——链路互相重叠串扰是物理必然（v1.1 #5），精确标定必须等空间部署完成。

## 3. 烧录与备份（现场改板/重烧时）

> 运行环境：`.bat` 在 cmd/资源管理器里跑，`.sh` 在 Git Bash 里跑。`flash_rx.bat`/`flash_tx.bat` 动手前先 chip_id 验物理 MAC，不对一律拒绝；`flash_tx.bat` 另有 node 身份防呆（校验构建产物与角色匹配的 `CIS_TX_NODE` 选择，见 §0.2）。`backup_flash.sh` 只做在位确认与芯片自动识别、**不比对 MAC**（按口整片备份，勿把口接错板）。

```bash
# 1) 覆盖任何板卡前先整片备份（§9.2 红线）
bash scripts/backup_flash.sh COM3 16M        # RX（S3，16MB）
bash scripts/backup_flash.sh COM15 4M        # TX1（经典 ESP32，4MB）

# 2) RX：脚本内置 chip_id 验 MAC（DC:DA:0C:1D:AE:10）→ build → flash
scripts\flash_rx.bat COM3

# 3) TX：按角色烧录（内置验 MAC + node 身份防呆；CH340 口不稳时加波特率参数）
scripts\flash_tx.bat tx1 COM15               # 经典#1，期望 MAC 8C:94:DF:A1:DD:D0（默认构建，node1）
scripts\flash_tx.bat tx2 COM5                # C3，期望 MAC E4:B0:63:33:F0:40
scripts\flash_tx.bat tx3 COM14 460800        # 经典#2，期望 MAC 8C:94:DF:A1:DA:4C（自动以 sdkconfig.defaults.tx3 构建 build_tx3，node3）
```

- 备份落在 `scripts/backup/<端口>_<芯片>_flash<容量>_<日期>.bin`，已存在同名文件时脚本拒绝覆盖。
- TX 换角色由脚本自动以对应 node 配置构建（tx1→默认 build、tx3→`build_tx3`，见 §0.2）；改 SSID 后须重构建（脚本自动做）。monitor 确认日志出现 `connected to AP 1003-k`、`node_id:` 与周期发包再拔线下地。
- TX 开机 monitor 日志里的 `mac=xx:xx:…` 即该板 CSI 源 MAC，抄下来填进验收记录表 §"链路登记"。

## 4. 上电与连接

1. 全部节点上电（TX 用短粗 USB 线或独立 5V，防供电抖动；§9.3）。
2. PC 打开 RX 串口 REPL（二选一）：
   - 串口：`idf.py -p COM3 monitor`（2M 波特）或上位机命令框；
   - 无线：PC 接入 1003-k 后 `telnet <RX的IP> 2323`（TCP REPL，banner 见 `cis_console.c:396`）。
3. 确认 RX 启动日志：`got ip` → `tcp console listening on port 2323` → 心跳 0x01 @1Hz。
4. `wifi_status` 确认已连 1003-k（NVS 有凭据时自动重连，无需再配）。

## 5. 每房间/每场地标定流程（§7.3 逐步版）

> 每条链路约 30~60s。REPL 命令按 `cis_console.c` 实测注册的语法。

### 5.1 登记链路（每块 TX 一次）

```
wifi_config -s 1003-k -p th880511     # 首次配置；之后 NVS 凭据自动重连
peer scan                             # 列最近 5min 活跃 CSI 源（仅已注册源可见，见 0.2）
peer add <tx源MAC> <区域名>            # 例: peer add 1a:00:00:00:00:01 门口
peer list                             # 核对：name_id=槽位号，0x10 帧携带
```

- `<tx源MAC>` 优先从该 TX 开机日志抄；`peer scan` 只对已注册源生效（0.2 红线）。
- peer 表容量 8（`cis_peer.h:17`），超出的链路需删旧再加。

### 5.2 标定一条链路（清场后执行）

```
# 1) 清场：所有人离开该链路覆盖区、关门、风扇空调关小、无移动物体，保持静止
train <tx源MAC> 15                    # 默认 15s（Kconfig CIS_TRAIN_SECONDS）
                                      # 回显: training started; 0x30 frame will follow when done
# 2) 等 15s，收 0x30 标定结果帧：mac + 训练时长 + 阈值 + 噪声底
#    固件日志同时打印: train done <mac>: noise=<N> c, threshold=<T> c (<windows> windows)
#    阈值规则: T = max(noise × 300%, 15) centi-var（cis_detect.c:294-305）
#    合理性核对（仿真依据，docs/算法仿真分析.md §3-S5）:
#      远链路 noise 基线 ≈ 220c（TX2 近场档 ≈ 6800c）；noise > 2×基线 → 训练被
#      污染（有人/物体动过），阈值会虚高 6 倍以上致走动全部漏检 → 清场重 train
dump <tx源MAC>                        # 核对 state/threshold/var 回读值
```

### 5.3 微调与持久化

```
threshold <tx源MAC> <val>             # 按验收结果 ±10~20% 微调（centi-var，RAM 生效）
threshold save                        # 全部阈值写入 NVS（不 save 断电即丢）
threshold <tx源MAC>                   # 无值 = 查询回读
```

- 微调方向：误报多 → 调高；灵敏度不足（挥手不报）→ 调低。每次只动一条链路，动完用 §5.4 复验。
- **禁止**在台架/未清场状态下为"看起来安静"调高默认阈值 30——台架共享信道噪声底本来就 >> 30，持续 MOTION 是预期（v1.3 #5），标定只认清场后的 `train` 结果。

### 5.4 单链路功能复验（标定完成即做）

1. 静止 30s：上位机 `dump`/特征曲线应平稳在阈值下方，无 EVENT；
2. 在链路覆盖区正常走动：2s 内应报 MOTION（0x10 事件，MAC 与区域名对应）；
3. 停走：≤3s 内 EVENT STILL（固件 M=25 窗 @10Hz 滞回）；
4. 结果记入 `docs/验收记录表.md` 对应用例行。

## 6. 换场地快速流程（§7.4）

1. 按 §1 检查新场地路由器（或跳到直收模式）；按 §2 重新摆位+拍照。
2. 每条链路：`peer add`（若未注册）→ 清场 → `train <mac> 15` → `threshold save` ≈ 30s。
3. 全链路完成后跑一轮 §5.4 复验。目标：备用场地 15min 内完成全部重标定（M5 排练判据）。

## 7. 无路由场地：直收模式（备用方案）

仅当答辩场地无法提供路由器/接入点时使用（`CONFIG_CIS_MODE_DIRECT`）：

1. RX：`idf.py menuconfig` → CIS Configuration → **Direct mode (fixed channel promiscuous RX)**，`CIS_DEFAULT_CHANNEL` 设为选定信道（默认 6）→ 重构建重烧（`flash_rx.bat`）。直收模式 RX 用 `esp_wifi_set_channel` 固定信道，不连 AP。
2. TX：改各自 `main/app_main.c`，`CONFIG_LESS_INTERFERENCE_CHANNEL` 设同一信道（该宏只影响直收路径；STA 模式信道跟随 AP 不硬编码，`csi_tx_v1/main/app_main.c:31`）→ 重烧。
3. 标定流程与 §5 相同（wifi_config 可跳过）；TCP REPL/UDP 上报在直收模式下不可用，全程走 COM3 串口。
4. 场地许可时提前排练一次，M5 判据"直收模式演练成功"。

## 8. 断电恢复检查（每次标定收尾必做）

1. RX 断电 → 重新上电 → 计时；
2. 判据：≤30s 内恢复检测（got ip → peer 表从 NVS 恢复 → 阈值从 NVS 恢复 → 特征流恢复）；
3. `peer list` + `threshold <mac>`（查询）逐条核对与断电前一致；
4. 不一致 = 忘了 `threshold save`，重做 §5.3。

## 9. 排错速查

| 现象 | 首查 | 处理 |
|---|---|---|
| RX 收不到某 TX | ① TX 是否还在发包（TX monitor）；② **信道是否一致**：`wifi_status` 看 RX 实际信道，TX 日志看 `connected with 1003-k, channel N`，两者必须相同（M2 曾因 RX 固定 6 而 AP 在 4 静默无数据） | 重连 AP 跟随信道，或路由器固定信道后两边重上 |
| `peer scan` 看不到新 TX | 精确过滤设计：注册首个 peer 后 scan 只显示已注册源 | 从 TX 开机日志抄源 MAC 直接 `peer add` |
| 台架下持续 MOTION | 共享信道噪声底 >> 默认阈值 30，属预期 | 清场后 `train`；禁止调默认阈值掩盖（§5.3） |
| 0x30 一直不来 | train 期间 RSSI 突变/丢包会丢窗重计；看 `dump` 的 `training=yes left=Xs` | 确认清场；必要时重跑 train |
| TCP 2323 / UDP 3333 连不上 | 网段隔离：PC 不在 1003-k 网段（v1.3 #3） | PC 接 1003-k LAN 或连 RX SoftAP；或直接用 COM3 串口 |
| 特征流空窗后恢复慢 | wifi_scan 跳扫会占用信道（实测空窗 ~2.6s 后恢复） | 标定/验收期间避免跑 `wifi_scan` |
| 烧录同步失败（经典板 CH340） | 口不稳/板过热 | 加 `-b 460800`（`flash_tx.bat` 第三参数）；凉板重插；仍失败按 0.1 验 MAC 后换口 |
| 两块 TX 事件混叠 | 角色配置用错（tx1 烧成 node3 或反之）：看开机日志 `node_id:` 与 `mac=`，对照 §0.2 身份表 | 以正确角色重烧（`flash_tx.bat` 构建产物 node 防呆会拦截错配构建） |

## 附 A. REPL 命令速查（cis_console.c 实测语法）

| 命令 | 说明 |
|---|---|
| `peer add <mac> [name]` | 注册链路，NVS 持久化 |
| `peer del <mac>` | 删除链路 |
| `peer list` | 列出全部 peer（threshold/state/var/mean/p2p/rssi） |
| `peer scan` | 最近 5min 活跃 CSI 源（仅已注册源） |
| `train <mac> [seconds]` | 静默标定（默认 15s），结束回 0x30 |
| `threshold <mac> [val]` | 设/查阈值（centi-var，RAM） |
| `threshold save` | 阈值写入 NVS |
| `report [on\|off\|rate <1~50>]` | 特征流开关/频率（默认 10Hz） |
| `udp <on [ip]\|off>` | UDP 3333 上报开关 |
| `dump [mac]` | 实时统计（state/thr/mean/var/p2p/windows/training/left） |
| `wifi_config -s <ssid> -p <pass>` | 配网，NVS 凭据 |
| `wifi_status` / `wifi_scan` | 连接状态 / 跳扫（标定时别用） |
| `system restart` / `ping` | 重启 / 连通性 |

## 附 B. 标定记录填写

每完成一条链路，把以下值抄进 `docs/验收记录表.md` 头部"链路登记"表：

- TX 物理 MAC / CSI 源 MAC / 区域名（name_id）
- `train` 回读：噪声底（noise c）、阈值（threshold c）、训练窗数
- 实际信道、TX 与 RX 摆位照片编号
