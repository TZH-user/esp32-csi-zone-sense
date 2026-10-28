/*
 * ESP32_CIS_V1.0 · cis_wifi.h — STA 连网 + NVS 凭据 + 重连；失败 30s 开 SoftAP 兜底配网
 *
 * 注意：esp_radar_init() 内部已完成 esp_wifi_init/start 并设好信道（实施计划 §4 的
 * 顺序 wifi→radar 在本框架下必须反转为 radar→wifi，因为 esp-radar 负责拉起 WiFi）。
 * 本模块只在 radar 之后做：读 NVS 凭据 → esp_wifi_connect()（异步任务）→
 * 30s 未获 IP → SoftAP（信道固定 CONFIG_CIS_DEFAULT_CHANNEL，避免挪走 CSI 接收信道）。
 * STA 一旦连上路由器，信道自动跟随 AP（v1.1：TX 也连同一路由器 → 同信道）。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/** NVS 凭据存储（components/commands wifi_cmd 在 `wifi_config -s -p` 时写入） */
#define CIS_WIFI_NVS_NS     "cis_wifi"
#define CIS_WIFI_NVS_SSID   "ssid"
#define CIS_WIFI_NVS_PASS   "pass"

/** 启动连网流程（异步任务，立即返回） */
void cis_wifi_start(void);

/** STA 是否已获 IP */
bool cis_wifi_connected(void);

/** 状态一行摘要（控制台 `wifi_status`） */
void cis_wifi_status(char *out, size_t len);

#ifdef __cplusplus
}
#endif
