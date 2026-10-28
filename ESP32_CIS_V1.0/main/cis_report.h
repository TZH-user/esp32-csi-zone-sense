/*
 * ESP32_CIS_V1.0 · cis_report.h — 二进制帧上报（串口主通道 + UDP 可选）
 * 帧格式见 cis_protocol.h（PC 端 protocol.py 唯一对照源）。
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 初始化并启动上报任务（帧序列化 + 0x01 心跳 1Hz + 0x10/0x11 节拍 + UDP）。 */
esp_err_t cis_report_start(void);

/* ---- 检测侧推送接口（非阻塞；队列满计丢帧） ---- */

/** 0x11 特征流候选值（report 任务按 CIS_FEATURE_RATE_HZ 节拍发送，最新值优先） */
void cis_report_feature(const uint8_t mac[6], uint32_t ts_ms,
                        uint16_t mean_c, uint16_t var_c, uint16_t p2p_c, int8_t rssi);

/** 0x10 事件帧（状态跳变即时发送；MOTION 持续期由 report 任务 2Hz 重发） */
void cis_report_event(const uint8_t mac[6], uint8_t state, uint16_t score_c,
                      int8_t rssi, uint32_t ts_ms);

/** 0x30 标定结果帧（同时由 report 任务把阈值持久化到 NVS peer 表） */
void cis_report_train_result(const uint8_t mac[6], uint16_t train_s,
                             uint16_t threshold_c, uint16_t noise_c);

/** 0x20 日志回显帧（ASCII，按需） */
void cis_report_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/* ---- 控制台命令接口 ---- */

void cis_report_set_enable(bool en);
bool cis_report_get_enable(void);
void cis_report_set_rate(uint8_t hz);       /* 1~50，钳制 */
uint8_t cis_report_get_rate(void);
esp_err_t cis_report_udp_set(bool en, const char *ip);  /* ip=NULL 保持原值（默认广播） */
void cis_report_udp_status(char *out, size_t len);
uint32_t cis_report_dropped(void);          /* 队列满 + 串口写失败丢帧数 */

/* ---- 通用帧发送（供其他模块按需发原始帧，如 TCP 控制台扩展） ---- */
esp_err_t cis_report_send_frame(uint8_t type, const void *payload, size_t len);

#ifdef __cplusplus
}
#endif
