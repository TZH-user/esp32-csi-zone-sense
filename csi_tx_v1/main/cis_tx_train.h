/*
 * csi_tx_v1 · cis_tx_train.h — per-source 静默标定（TX 观测侧）+ NVS 阈值持久化
 *
 * 逻辑对照 ESP32_CIS_V1.0/main/cis_detect.c:282-307：训练期间逐窗累计 var_c →
 * 噪声底 = 窗方差均值（centi-var）→ 阈值 = 噪声底 × margin%（下限钳制）。
 * 触发不再依赖 RX/S3 下发（CONSOLE `train` 或 CMD_TRAIN 单播），标定结果按
 * 源 MAC 持久化在 NVS 命名空间 cis_trx，开机自动恢复——TX 断电重启后阈值
 * 仍在，S3 无需重新下发（CALIB_RSP 仅在上行链路通报结果）。
 *
 * 刻度：threshold/noise 为 centi-var（raw² × 100），与 0x11/0x12 的 var 字段
 * 同刻度；因 TX 与 S3 幅值刻度不同（见 cis_csi_rx.h 刻度差异注记），每条
 * (observer, source) 链路必须独立训练，阈值不可跨 observer 复用。
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 训练参数（对齐 RX 工程 Kconfig 默认值 CIS_TRAIN_SECONDS/MARGIN_PCT/MIN_THRESHOLD） */
#define CIS_TX_TRAIN_DEFAULT_S       15u    /* CMD_TRAIN seconds=0 时采用 */
#define CIS_TX_TRAIN_MARGIN_PCT      300u   /* 阈值 = 噪声底 × 300% */
#define CIS_TX_TRAIN_MIN_THRESHOLD   15u    /* 阈值下限（centi-var） */

/* NVS：命名空间 cis_trx，键 = 源 MAC 12 位十六进制，值 = 持久化 blob */
#define CIS_TX_TRAIN_NVS_NS   "cis_trx"

/* 一次完成的训练结果（CALIB_RSP 载荷同构） */
typedef struct {
    uint8_t  mac[6];
    uint16_t train_s;          /* 本次训练设定时长（秒） */
    uint16_t threshold_c;      /* centi-var */
    uint16_t noise_c;          /* 噪声底，centi-var */
} cis_tx_train_result_t;

/** 初始化：注册排窗回调 + 从 NVS cis_trx 恢复各白名单源阈值。须在
 *  cis_csi_rx_init 之后、cis_csi_rx_start 之前调用。 */
esp_err_t cis_tx_train_init(void);

/** 启动某源的静默标定（同源已有训练进行中返回 ESP_ERR_INVALID_STATE）。
 *  期间须清场：训练把环境扰动当噪声底，污染即阈值虚高。 */
esp_err_t cis_tx_train_start(const uint8_t mac[6], uint16_t seconds);

/** 轮询取一条已完成的训练结果（完成即从就绪队列弹出）。此调用同时把结果
 *  写入 NVS cis_trx（本函数在 cis_now 打包任务上下文执行，低优先级写 NVS，
 *  避免在 esp_timer/CSI 回调里落盘）。无完成项返回 false。 */
bool cis_tx_train_poll_done(cis_tx_train_result_t *out);

/** 当前生效阈值（centi-var；未训练过/未持久化返回 0）。 */
uint16_t cis_tx_train_get_threshold(const uint8_t mac[6]);

#ifdef __cplusplus
}
#endif
