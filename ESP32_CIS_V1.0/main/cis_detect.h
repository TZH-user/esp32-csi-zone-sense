/*
 * ESP32_CIS_V1.0 · cis_detect.h — per-peer 滑窗统计 + 滞回去抖 FSM（自管上下文）
 *
 * 实施计划 §4：不依赖 esp_radar_motion_dec 单例。信号链：
 *   cis_detect_csi_cb      —— esp-radar 逐包回调（WiFi 上下文）：算单包平均幅值（毫单位）、
 *                             RSSI 突变过滤（|ΔRSSI|>6dB 的包不进滑窗，esp-csi issue #185）、
 *                             按 MAC 累加窗统计（均值/平方和/极值），并喂 cis_peer 观测表；
 *   cis_detect_radar_cb_ex —— esp-radar 检测窗回调（≈10Hz/peer）：排出窗口统计
 *                             （均值/方差/峰峰值），跑滞回 FSM，事件/特征推给 cis_report。
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"
#include "esp_radar.h"   /* wifi_csi_filtered_info_t / wifi_radar_info_t（无 tag typedef，不能前向声明） */

#ifdef __cplusplus
extern "C" {
#endif

/* 检测状态（与 cis_protocol.h CIS_STATE_x 一致） */
#define CIS_DET_IDLE      CIS_STATE_IDLE
#define CIS_DET_MOTION    CIS_STATE_MOTION
#define CIS_DET_PRESENCE  CIS_STATE_PRESENCE

typedef struct {
    uint8_t  mac[6];
    uint8_t  state;             /* CIS_DET_x */
    uint16_t threshold;         /* centi-var 刻度 */
    bool     training;
    uint16_t train_total_s;     /* 本次训练设定时长 */
    uint16_t train_left_s;      /* 剩余秒数 */
    uint32_t mean_c;            /* 最近窗口特征（centi 刻度，见 cis_protocol.h） */
    uint32_t var_c;
    uint32_t p2p_c;
    int8_t   rssi;
    uint32_t ts_ms;
    uint32_t windows;           /* 累计窗口数 */
    uint32_t dirty_windows;     /* 因 RSSI 突变被丢弃的窗口数 */
} cis_det_stats_t;

/** 模块初始化（创建上下文池）。须在 esp_radar_init 前调用。 */
void cis_detect_init(void);

/** 启动（当前为标记位；处理直接在雷达回调上下文完成，无独立任务）。 */
void cis_detect_start(void);

/** 确保 per-peer 上下文存在（不存在则创建），并更新阈值（threshold_c=0 表示用默认）。 */
esp_err_t cis_detect_peer_ensure(const uint8_t mac[6], uint16_t threshold_c);

/** 释放 per-peer 上下文（peer del 时调用）。 */
esp_err_t cis_detect_peer_remove(const uint8_t mac[6]);

/** 修改阈值（centi-var）。 */
void cis_detect_set_threshold(const uint8_t mac[6], uint16_t threshold_c);

/** 启动静默标定：收集 seconds 秒（默认 CONFIG_CIS_TRAIN_SECONDS）窗方差 → 噪声底 →
 *  阈值 = 噪声底 × CONFIG_CIS_TRAIN_MARGIN_PCT/100（下限 CIS_TRAIN_MIN_THRESHOLD），
 *  完成后回 0x30 帧并由 cis_report 持久化。同一时刻每 peer 只允许一个训练。 */
esp_err_t cis_detect_train_start(const uint8_t mac[6], uint16_t seconds);

/** 供 esp_radar_init 注册：逐包 CSI 回调 */
void cis_detect_csi_cb(void *ctx, const wifi_csi_filtered_info_t *info);

/** 供 esp_radar_init 注册：检测窗回调（带源 MAC） */
void cis_detect_radar_cb_ex(void *ctx, const uint8_t mac[6], const wifi_radar_info_t *info);

/** dump：取 per-peer 统计快照（mac 未注册返回 ESP_ERR_NOT_FOUND） */
esp_err_t cis_detect_get_stats(const uint8_t mac[6], cis_det_stats_t *out);

/** 丢帧计数（RSSI 突变丢窗 + 特征队列满丢弃），0x01 心跳 lost_frames 字段 */
uint32_t cis_detect_lost_frames(void);

#ifdef __cplusplus
}
#endif
