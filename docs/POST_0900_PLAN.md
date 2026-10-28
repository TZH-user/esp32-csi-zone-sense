# 条件性补充准备与验证计划（触发条件：2026-10-28 09:00 前工作流 M1~M4 完成）

> 依据用户 2026-10-28 凌晨指示："如果 28 日 09:00 前完成前面任务，就考虑完成其他准备工作或验证工作，别现在做"。
> 执行者：09:00 定时任务（或任何确认条件成立的会话）。用户不在场，全部条目为无需人在场、PC 本地可完成的只读或写文档工作。

## 0. 前置条件检查（不成立则只出简报，不执行任何项）
1. 用 GetWorkflowRun 查 `dwfrun-1390a201-05eb-4563-b260-0c79e5c9a0e9`：`status=completed`，或里程碑看板 M1/M2/M3/M4 四卡均 `passed` → 条件成立；
2. 工作流仍在运行或 stopped/errored → 只输出状态简报退出，**不干预、不修改脚本**（errored 时另行走 AmendWorkflow 修复路径，但那不属于本计划）。

## A. 验证类（只读/本地复跑，优先做）
> 进度（2026-10-28 02:30 主会话）：A1/A2/A4 已完成✅，B1 已完成✅；A3/A5/A6/B2 待工作流结束后由 09:00 定时任务执行；B3 已移出。
> 补充（2026-10-28 03:05 主会话）：A3 已提前完成（ui_smoke 为 SimSource 模拟源、不开 COM3，无需等工作流释放串口）；B2 已提前完成（见 B2 行）；protocol.py decode_payload 增加显式 payload 长度守卫（struct.error 非 ValueError 子类，原实现会让 minimal_monitor 的 EVENT 分支在"CRC 过但长度错"帧上崩溃），改后 selftest 复跑 20/20 无回归、错误路径与正常路径双向实测通过。
> 工作流进展（2026-10-28 02:40 主会话）：P1/M1/M2/M3 六阶段全部完成，M3 冒烟门单轮通过（1m01s 未触发修复环，即 ui_smoke 退出码 0）；M4 按用户既定指示以两链路收尾（TX1=经典#1 COM15 / TX2=C3 COM5），经典#2（MAC 8C:94:DF:A1:DA:4C，COM14）芯片进不了下载模式（esptool default_reset 与 no_reset 均 "No serial data received"，疑似过热卡死），已列为遗留人工项：用户回场后冷却/重插 → chip_id 复验 MAC → 烧 fw_idle 轻载固件 → 有任务需要时再换烧 csi_tx_v1 补第三链路。

| # | 项 | 方法 | 通过判据 | 状态 |
|---|---|---|---|---|
| A1 | protocol_selftest 独立复跑 | IDF venv Python 跑 `pc_dashboard\protocol_selftest.py` | 退出码 0 | ✅ 2026-10-28 02:15：20/20 全过，退出码 0；03:05 protocol.py 加长度守卫后复跑仍 20/20 无回归 |
| A2 | 协议双端一致性 | 逐字段比对 `pc_dashboard\protocol.py` ↔ `ESP32_CIS_V1.0\main\cis_protocol.h`（帧头/ver/type/len 字节序/CRC 参数与覆盖范围/0x01·0x10·0x11·0x30 各 payload 字段顺序与宽度） | 完全一致，差异列表为空 | ✅ 2026-10-28 02:15：零差异（CRC=CCITT-FALSE poly 0x1021/init 0xFFFF 覆盖 ver..payload；payload 11/15/17/12B 字段序全对；centi 刻度约定一致） |
| A3 | ui_smoke 复跑 | 跑 `pc_dashboard\ui_smoke.py`（timeout 180s） | 退出码 0 | ✅ 2026-10-28 03:02：`--offscreen` 复跑 15/15 全 PASS、退出码 0（SimSource 模拟源不占 COM3；结构检查/标定倒计时/0x30 回填/事件竖线/CSV 列齐全与行数/回放缓冲全过） |
| A4 | M2 固件代码走查 | 读 `cis_detect.c/h`、`cis_report.c/h`：①是否丢弃 |ΔRSSI|>6dB 的帧（计划 v1.1 第 6 条）；②是否自管 per-peer 上下文、不依赖 motion_dec 单例；③帧格式与计划 §5 一致 | 三项全满足或差异记录 | ✅ 2026-10-28 02:20：三项全过；RSSI 过滤增强为"丢包+整窗 dirty+计数"，训练期暂停 FSM，NVS 写移出雷达回调；注记：ACC 槽位=8 上限、sqrtf 逐子载波（M5 后可选 esp-dsp 优化） |
| A5 | 工作流结论复核 | 读最终 WorkflowReport：verified 项抽查证据（重跑 1~2 条命令）；unconfirmed 项逐条尝试只读复核 | 复核结论附简报 | ✅ 2026-10-28 03:45：抽查三项全对——①scripts/backup/ 三片全片备份尺寸精确吻合（COM3 16,777,216B / COM5·COM8 各 4,194,304B）；②docs/m4_restart_nvs_run4.log 实证 rst:0xc 软重启 → 双 peer 带阈值恢复（681/20367）@1.26s → IP@4.9s；③scripts/c3_monitor_COM5_20261028.log 实证 TX2 源 MAC 1a:00:00:00:00:01 连 1003-k 信道 4。unconfirmed 项中可只读复核的（源 MAC、信道、端口漂移、备份）已全部复核属实；需人/需硬件的保持遗留（走动扰动、真人遮挡、物理断电、RX 两次非指令重启归因——brownout 仅为推测未证实） |
| A6 | 数据流抽样复验（仅当 COM3 空闲） | 60s 只读抓 COM3@2M，对比 `docs\m1_sample.log` 的帧率/MAC/幅值统计 | 帧率量级一致、MAC 稳定 | ✅ 2026-10-28 03:50：`scripts/m2_verify.py COM3 2000000 60 docs/a6_m2_0350.log`——bad_crc=0、resyncs=0、心跳 59 帧（uptime 间隔 1000~1049ms）、0x11 特征流 1082 帧（…:00/…:01 各 541 ≈9Hz/peer），MAC 稳定=判据过。注意：幅值（mean≈4.1~4.5 raw）与 RSSI（-11/-1）与 M1 基线（37.93/-6）不同，系 M4 期间台架被移动所致（工作流 findings 已记录），空间部署后须按 SOP 重标定 |

## B. 准备类（答辩/交付）
| # | 项 | 产出 |
|---|---|---|
| B1 | 工程 README.md | 结构说明、快速复现命令（烧 RX/TX、启动上位机）、协议表摘要、目录说明 |
| B2 | 交付文档核对补漏 | 检查 M3 产出的 docs/标定SOP.md、docs/验收记录表.md、scripts/flash_*.bat 是否齐全可执行，缺项按 IMPLEMENTATION_PLAN §7/§8 补齐 | ✅ 2026-10-28 03:00 主会话提前完成（Explore 只读核对+主会话修订）：①标定SOP §0.2 事实纠错（TX2 源 MAC 实为 …:01 且已区分，删除"两工程同 MAC"过时表述，改写 TX3 复用 csi_tx_v1 的混叠风险与操作步骤）；②§3/L66 措辞修正（backup_flash.sh 不比对 MAC）；③§9 排错表混叠行补 tx3 提示；④验收记录表补"上位机 30min 长稳"记录行（随用例①同跑）+总判定"另记"行；⑤flash_tx.bat 新增源 MAC 归属防呆（tx1 要求 …:00、tx3 拒绝 …:00，findstr 模式已实测命中）。低优先未做：RX sdkconfig 生成态 115200 与文档 2M 口径注记（USB-Serial-JTAG 下无实效） |
| B3 | ~~答辩演示大纲~~ **已移出**（用户 2026-10-28 指示：答辩内容等实际验收后再做） | 不执行，留待用户验收后 |
| B4 | 证据归档检查 | m1_sample.log、工作流报告、CSV 样例均已落 docs/ 或 data/，路径在简报中列出 | ✅ 2026-10-28 03:55：docs/（m1_sample*.log、m2_verify_run*.log、m4_register_train_run1.log、m4_restart_nvs_run4.log、a6_m2_0350.log、CSI_SURVEY.md、标定SOP.md、验收记录表.md）、scripts/backup/（3 片全片备份）、scripts/（c3_monitor_COM5、tx1_monitor_COM15 系列）、data/（冒烟 CSV 在临时目录；data/20261028_0236 空表头目录为 CLI 超时击杀残留，可删） |

## C. 禁止项（硬边界）
- 不插拔、不烧录任何板卡（用户不在场；经典#2 的 fw_idle 等用户回场后再烧）；
- 不执行任何需要人的验收项（走动测试、清场标定）——只整理"待人工执行清单"；
- 不改工作流脚本、不 AmendWorkflow、不重启工作流；
- 端口操作一律先按 MAC 锚定（esptool chip_id 验 MAC），且仅在确认工作流已结束、无子代理占用端口时进行。

## D. 完成定义
A、B 全部可执行项完成后，输出简报：每项 结论+证据（命令退出码/文件路径/差异清单），以及"待人工执行清单"（走动演示、清场标定、M5 七用例、30min 长稳、经典#2 烧 fw_idle）。
