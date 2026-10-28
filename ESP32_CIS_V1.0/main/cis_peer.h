/*
 * ESP32_CIS_V1.0 · cis_peer.h — MAC→区域名 peer 表 + NVS 持久化 + 最近源 MAC 扫描表
 * 实施计划 §4：`peer add/del/list/scan`；断电后由 cis_peer_load() 恢复并重建
 * esp-radar peer 与检测上下文。
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CIS_PEER_MAX        8       /* peer 表容量（= name_id 取值上限） */
#define CIS_PEER_NAME_LEN   16      /* 区域名最长字符数（含 0） */

typedef struct {
    uint8_t  mac[6];
    char     name[CIS_PEER_NAME_LEN];   /* 区域名，如 "door" "sofa" */
    uint16_t threshold;                 /* centi-var 刻度（cis_protocol.h），0=用默认 */
    bool     used;
} cis_peer_entry_t;

/* 名字 id = 在表中的槽位号（0..CIS_PEER_MAX-1），0x10 帧携带该 id */

/**
 * @brief 启动时从 NVS 恢复 peer 表，并对每个 peer 调 esp_radar_new_peer + cis_detect_peer_ensure。
 *       必须在 esp_radar_init() 与 cis_detect_init() 之后调用。
 */
void cis_peer_load(void);

/** 添加 peer（MAC 未在表内时）；成功即写 NVS。name 可为 NULL（默认 "zoneN"）。 */
esp_err_t cis_peer_add(const uint8_t mac[6], const char *name);

/** 删除 peer（esp_radar_stop/del_peer/start 由内部完成，短暂中断检测）；成功即写 NVS。 */
esp_err_t cis_peer_del(const uint8_t mac[6]);

/** 查找：返回槽位号，找不到返回 -1 */
int cis_peer_find(const uint8_t mac[6]);

/** 按槽位号取条目；越界/未用返回 NULL */
const cis_peer_entry_t *cis_peer_get(int slot);

/** 当前已用 peer 数（0x01 心跳 peer_count 字段） */
uint8_t cis_peer_count(void);

/** 设置并持久化阈值（centi-var） */
esp_err_t cis_peer_set_threshold(const uint8_t mac[6], uint16_t threshold_c);

/** 把内存表整体写 NVS（`threshold save` 用） */
esp_err_t cis_peer_save(void);

/* ---- peer scan：最近 CSI 源 MAC 观测表（cis_csi_cb 每包更新） ---- */

/** 记录一包 CSI 的源 MAC / RSSI（WiFi 回调上下文安全，短临界区） */
void cis_peer_note_csi(const uint8_t mac[6], int rssi);

/** 遍历最近观测：返回第 i 个活跃 MAC（按最近活跃排序）；i 越界返回 false。
 *  age_ms/last_rssi 可为 NULL。 */
bool cis_peer_scan_get(size_t i, uint8_t mac_out[6], int *last_rssi, uint32_t *age_ms);

#ifdef __cplusplus
}
#endif
