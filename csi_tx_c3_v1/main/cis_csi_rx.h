/*
 * csi_tx_c3_v1 · cis_csi_rx.h — TX 节点本地 CSI 观测（mesh observer 角色）
 * 同步源 = csi_tx_v1/main/cis_csi_rx.h（两工程无共享组件机制，改动须双写；与源差异仅此文件头）
 *
 * 职责（自管，不引入 esp-radar 组件——其 esp_radar_init 强制自带
 * esp_wifi_init/start（esp_radar.c:1848-1853），与本工程自管 WiFi 双初始化冲突）：
 *   1) 按 esp-radar 对 esp32/c3 同一分支的 4 连调打开 CSI 直收
 *      （esp_wifi_set_promiscuous(true) + set_csi_config + set_csi_rx_cb +
 *      set_csi(true)，照抄 esp_radar.c:1608-1626 与其 CSI 配置默认值）；
 *   2) 软件白名单过滤：源 MAC ∈ {其余 3 节点}（对端 TX 源帧 + S3 源帧）；
 *   3) per-MAC 逐包累加器 + RSSI 突变过滤（移植 ESP32_CIS_V1.0 cis_detect.c:44-61,
 *      130-208，槽 4），单包平均幅值 = mean(|I+jQ|)（毫单位，M1 同源算法）；
 *   4) CIS_CSI_WINDOW_MS esp_timer 周期排窗（替代 esp-radar radar_cb_ex），
 *      产出 (mean_c, var_c, p2p_c, rssi, ts)，单位刻度与 0x11 完全一致
 *      （centi-amp / centi-var，见 ESP32_CIS_V1.0 cis_protocol.h 单位约定）。
 *
 * == 刻度差异注记（跨 observer 只比相对量）==
 * 本模块在 TX 上用【原始 wifi_csi_info_t 的全 tone I/Q】（int8 交替，len/2 对）
 * 计算单包平均幅值；S3 侧用【esp-radar 重建的 L-LTF 子载波集】
 * （esp_radar.c:593-622 esp_radar_rebuild_csi_data / sub_carrier_table，仅取
 * L-LTF 有效子载波段）。两处覆盖的子载波集合与归一不同 → 绝对幅值刻度不同，
 * 同一物理信道在 TX/S3 的 mean/var 数值不可直接互换：
 *   - 跨 observer 只比较相对量（var 的相对变化、超阈倍数）；
 *   - 每条 (observer, source) 链路独立 train 归一（cis_tx_train + 0x14 标定帧
 *     即为此设计），阈值/噪声底只在同 observer 同链路内比较。
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 白名单槽位：3 个对端节点 + 1 余量 */
#define CIS_CSI_SRC_MAX  4

/* 一次排窗产出（centi 刻度，同 0x11；ts_ms = esp_timer 毫秒） */
typedef struct {
    uint8_t  mac[6];
    uint32_t mean_c;            /* 窗均幅值，centi-amp */
    uint32_t var_c;             /* 逐包窗方差，centi-var */
    uint32_t p2p_c;             /* 窗内峰峰值，centi-amp */
    int8_t   rssi;              /* 窗内最后一包 RSSI, dBm */
    uint32_t ts_ms;             /* 排窗时刻 */
} cis_csi_feat_t;

/** 排窗回调（esp_timer 任务上下文，必须轻量：只做数值累计，勿做 NVS/发送） */
typedef void (*cis_csi_window_fn)(const cis_csi_feat_t *feat);

/** 模块初始化：构建白名单（其余 3 节点 MAC，S3 取 CONFIG_CIS_MESH_S3_MAC）、清零累加器。 */
esp_err_t cis_csi_rx_init(void);

/** 打开 CSI（4 连调）+ 启动 CIS_CSI_WINDOW_MS 排窗 esp_timer。须在 wifi_init 之后调用。 */
esp_err_t cis_csi_rx_start(void);

/** 取白名单源 MAC 表（返回条数，最多 max 条）。 */
int cis_csi_rx_sources(uint8_t out[][6], int max);

/** 源 MAC 是否在白名单（其余 3 节点）。 */
bool cis_csi_rx_source_allowed(const uint8_t mac[6]);

/** 取某源的最新窗口快照（无窗/源不在白名单返回 ESP_ERR_NOT_FOUND）。
 *  快照非消费式：同一窗可被打包任务与日志分别读取，去重靠 ts_ms。 */
esp_err_t cis_csi_rx_get_feature(const uint8_t mac[6], cis_csi_feat_t *out);

/** 注册排窗回调（训练模块用；仅一个，后注册覆盖前者）。须在 start 前调用。 */
void cis_csi_rx_set_window_hook(cis_csi_window_fn fn);

/** 因 RSSI 突变被整窗丢弃的累计窗数（诊断）。 */
uint32_t cis_csi_rx_dropped_windows(void);

#ifdef __cplusplus
}
#endif
