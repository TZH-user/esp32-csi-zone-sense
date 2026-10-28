# ESP32 CSI 感知 · 开源项目与论文调研

> 调研日期：2026-10-28。目的：对照业界做法校验本工程（题目3 ESP32 CSI 多监测点扰动监控）的技术路线，并挖掘可升级的算法方向。所有链接实测可达。

## 1. 结论摘要

1. 本工程的技术路线（CSI 幅值滑窗统计 + 阈值 + 滞回 FSM + 现场标定）与业界主流做法一致，无方向性风险。
2. 最值得借鉴的算法升级点：**ESPectre 的"标定期筛选高方差子载波 + 滑动方差"**，比全子载波平均抗干扰更强，可作为 M5 后的增强项。
3. 最大的已知工程陷阱是 **AGC 增益跳变**：ESP32 的 CSI 不携带 AGC 信息，增益切换造成的是乘性幅值突变，无法按加性噪声滤掉。Espressif 官方对策组件 `esp_csi_gain_ctrl` 已随 esp-radar 缓存在本工程中，检测端还应叠加"RSSI 突变帧丢弃"逻辑。
4. 学术界已证实廉价 ESP32 的极限：**多人/多目标分辨是公认弱项**——M5 验收预期要按"每链路独立检测"管理，不承诺区分人。
5. IEEE **802.11bf（Wi-Fi Sensing）已于 2025-05-29 正式批准**，答辩时可作为"该方向已是行业标准"的背景论据。

## 2. 开源项目对照

| 项目 | 定位 | 与本工程的关系 |
|---|---|---|
| [espressif/esp-csi](https://github.com/espressif/esp-csi)（官方） | CSI 采集 + 人存在检测/室内定位应用示例，esp-radar 组件 | **本工程直接基座**（esp-radar 0.3.4 多 peer） |
| [StevenMHernandez/ESP32-CSI-Tool](https://github.com/StevenMHernandez/ESP32-CSI-Tool)（[官网](https://stevenmhernandez.github.io/ESP32-CSI-Tool)） | 学术界经典 CSI 采集框架，主动/被动双模式 | 采集思路参照；其作者的综述见 §3[1] |
| [francescopace/espectre](https://github.com/francescopace/espectre)（[espectre.dev](https://espectre.dev)，另见[组件注册表示例](https://components-staging.espressif.com/components/francescopace/espectre/versions/3.0.0-rc3-25-ge16c657.main/examples/wifi_motion_detection?language=en)） | ESPHome/HA 生态的运动检测：**标定期选最高方差子载波 + 滑动方差 + 阈值** | **算法升级方向**（§4-2）；GPLv3 注意许可 |
| [RuView](https://ruview.blog/wifi-presence-detection) / [Cognitum 基准页](https://cognitum.one/learn/understanding-wifi-csi-sensing-benchmarks) | 存在检测科普 + 基准（ESP32 CSI 达 92.9% PCK@20） | 验收指标参照 |
| GitHub 主题索引 [device-free-sensing](https://github.com/topics/device-free-sensing) / [wireless-sensing](https://github.com/topics/wireless-sensing) / [WhoFi 索引](https://github.com/barleyjohn/whofi/blob/main/GitHub_Repository_Index.md) | 更多 ESP32 CSI+ML 仓库入口 | 后续扩展检索用 |

## 3. 论文清单

1. **Hernandez & Bulut,《WiFi Sensing on the Edge: Signal Processing Techniques and Challenges for Real-World Systems》, IEEE Communications Surveys & Tutorials, vol.25, 2022**。[PDF](https://ebulutvcu.github.io/COMST22_WiFi_Sensing_Survey.pdf)（32 页）。要点：幅值/相位是最基础特征；边缘设备上做感知的算力与实时性挑战；作者即 ESP32-CSI-Tool 作者。
2. **Ma, Wang, Geng,《WiFi Sensing with Channel State Information: A Survey》, ACM Computing Surveys, 2019**。CSI 信号处理与应用（运动/呼吸/身份）全景。[PDF 镜像](https://gzhou.pages.wm.edu/papers/WiFiSenseSurvey_CSUR19.pdf)。
3. **Du et al.,《An Overview on IEEE 802.11bf: WLAN Sensing》, 2022**。[arXiv](https://ui.adsabs.harvard.edu/abs/2022arXiv220704859D/abstract)。802.11bf 最常引概述；**该标准 2025-05-29 已批准**（[时间线](https://wirelesswithwade.com/ieee-802-11-standards-timeline)、[Origin Wireless 解读](https://www.originwirelessai.com/the-future-of-wifi-sensing-a-look-at-the-upcoming-ieee-802-11bf-standard)）。
4. **《A Survey on Wi-Fi Sensing Generalizability》, 2025**。[arXiv 2503.08008](https://arxiv.org/html/2503.08008v3)。跨场地/跨设备泛化难——支撑本工程"换场地必重标定"的 SOP。
5. **《Why Commodity WiFi Sensors Fail at Multi-Person Gait》, 2026-01**。[arXiv 2601.02177](https://arxiv.org/html/2601.02177v1)。实测 ESP32 5GHz CSI 幅值在多人步态任务上失败——**管理多节点/多人预期**的直接证据。
6. **《CSI Simulation: Why Additive Noise Fails and How to Fix It》**。[arXiv 2607.01882](https://arxiv.org/html/2607.01882v1)。AGC 乘性压缩机理：增益跳变不能当加性噪声滤。
7. Minho 大学硕士论文（Matey-Sanz）：ESP32 CSI 时间稳定性与 HAR 阈值漂移。[PDF](https://repositorium.uminho.pt/bitstreams/133497d4-c128-4f86-8e05-d0eb8893ad78/download)。

## 4. 对本工程的具体启示

1. **AGC 增益跳变（最高优先级陷阱）**：[esp-csi issue #185](https://github.com/espressif/esp-csi/issues/185) 指出 ESP32 CSI 不带 AGC 信息；官方 [ESP-CSI 方案文档](https://docs.espressif.com/projects/esp-techpedia/en/latest/esp-friends/solution-introduction/esp-csi/esp-csi-solution.html) 提供 RF 增益补偿组件（本工程 managed_components 已含 `esp_csi_gain_ctrl`）。检测端建议：**丢弃 RSSI 突变帧**（如 |ΔRSSI|>6dB 的帧不进滑窗），避免增益切换误报为运动。→ 已加入 M2 检测状态的实现要点。
2. **算法升级路线（对标 ESPectre）**：标定期对每子载波算方差 → 选 top-K 高方差子载波 → 只对这 K 个算滑动方差 → 阈值判运动。比全子载波平均的信噪比更高。当前 cis_detect 用全子载波幅值 mean/var/p2p 是合理的 v1，M5 后可作为增强实验点（答辩加分项）。
3. **预期管理**：多人步态在 ESP32 上失败（§3-5）→ 本工程承诺的是"**每条链路**（每个监测区）的运动/存在状态"，不是"区分几个人"；隔墙检测允许降级——M5 用例表已如此设计。
4. **部署敏感性**：房间几何/家具/天线位置都进 CSI（[RuView](https://ruview.blog/channel-state-information-wifi-sensing)、[DiVA 实测研究](https://www.diva-portal.org/smash/get/diva2:2036482/FULLTEXT02)）→ 印证"标定后挪动即重 train"与"胶带标记+拍照"的 SOP 必要性。
5. **答辩话术素材**：802.11bf 2025 年转正说明该方向已是行业正式标准；本工程在廉价 ESP32 上用官方组件实现了标准所定义的感知能力的最小可用子集，并有完整标定/验收流程。

## 5. 检索方法备注

检索于 2026-10-28，关键词组合：`ESP32 CSI open source`、`WiFi CSI HAR survey 2024/2025`、`ESP32 CSI motion detection threshold calibration`、`802.11bf status`、`ESPectre calibration subcarrier`、`esp-csi AGC gain jump`。中英文混合检索，英文文献覆盖为主。
