/*
 * ESP32_CIS_V1.0 · cis_mesh.h — ESP-NOW 多节点 mesh（S3 RX 汇聚，多节点升级 2026-10）
 *
 * 角色与方向：
 *   S3 每 CONFIG_CIS_MESH_SRC_RATE_HZ Hz 广播 4B 计数器（与 TX 板 csi_send 源帧同构，
 *   供其它节点把 S3 当 CSI 源）；TX 以 CONFIG_CIS_MESH_FEATURE_RATE_HZ Hz 单播
 *   FEAT_BUNDLE 给 S3；S3 经 cis_mesh_train 转发 CMD_TRAIN；TX 训练完成后回 CALIB_RSP。
 *
 * ESP-NOW 载荷帧内格式（自描述；802.11 FCS 之外不再加 CRC，magic+seq 供防串包/丢包统计）：
 *   magic 0xC5 0x5A | ver(1) | type(1) | seq u16 LE（每发送方滚动）| node_id(1)=
 *   发送方 CSI 源 MAC[5] | nrec(1)，头 8B；
 *   type 0x01 FEAT_BUNDLE : nrec×17B = source_mac(6)|ts_ms(4)|mean(2)|var(2)|p2p(2)|rssi(1)
 *                           （与串口 0x12 去掉 observer 后同构；ts_ms 为发送方本地 uptime）
 *   type 0x02 CALIB_RSP   : mac(6)|train_s(2)|threshold(2)|noise(2)
 *   type 0x10 CMD_TRAIN   : mac(6)|seconds(2)（S3→TX 单播）
 *   单包上限 ESP_NOW_MAX_DATA_LEN=250 → nrec≤14（实际 10Hz×3 链路=3 条/包，59B）。
 * 不加密（与 TX 现状 esp_now peer encrypt=false 一致）；peer channel=0 随 STA。
 *
 * 启动时序约束：cis_mesh_start 必须在 esp_radar_init 拉起 WiFi 之后、STA connect
 * （cis_wifi_start）之前调用——esp_wifi_set_mac 需 WiFi 已启动，且 observer 身份
 * MAC（1a:00:00:00:00:CONFIG_CIS_NODE_SELF_ID）必须在 connect 前生效。
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"
#include "cis_protocol.h"   /* CIS_LINK_MAX / CIS_OBSERVER_SELF / cis_link_slot */

#ifdef __cplusplus
extern "C" {
#endif

/* ESP-NOW mesh 帧内常量（PC 端无对照，仅固件/未来 TX 固件使用） */
#define CIS_MESH_MAGIC0          0xC5
#define CIS_MESH_MAGIC1          0x5A
#define CIS_MESH_VER             1
#define CIS_MESH_TYPE_FEAT_BUNDLE 0x01
#define CIS_MESH_TYPE_CALIB_RSP   0x02
#define CIS_MESH_TYPE_CMD_TRAIN   0x10
#define CIS_MESH_HDR_LEN         8
#define CIS_MESH_REC_LEN         17   /* FEAT_BUNDLE 单记录 */
#define CIS_MESH_MAX_REC         14   /* (250-8)/17 */

/** 启动 mesh：设本节点源 MAC → esp_now_init + PMK + peer 表（ff:ff 广播 + 各 TX
 *  单播）→ 12 链路表（cis_link NVS 阈值回填 + 远端检测上下文）→ 100Hz 广播任务 +
 *  mesh 解析任务。重复调用安全。CONFIG_CIS_MESH_ENABLE=n 时空操作。 */
esp_err_t cis_mesh_start(void);

/** 标定下发：observer=CIS_OBSERVER_SELF 时本地 cis_detect_train_start；否则把
 *  CMD_TRAIN 单播给对应 observer（其训练完成后回 CALIB_RSP → 0x14 帧回填）。 */
esp_err_t cis_mesh_train(uint8_t observer, const uint8_t mac[6], uint16_t seconds);

/** 链路快照（`link list` 用）。i ∈ [0, CIS_LINK_MAX)。
 *  本地链路（observer=self）实时状态取自 cis_detect（peer 注册后才有）；远端链路
 *  取自 mesh 链路表。age_ms 为 S3 本地时钟的"最近更新距今"，跨链路可比。 */
typedef struct {
    uint8_t  observer;
    uint8_t  mac[6];
    bool     active;        /* 收到过窗口特征（本地链路=已产出过窗口） */
    bool     registered;    /* observer=self 时：本地 peer 表已注册（有检测上下文） */
    uint16_t threshold;     /* centi-var */
    uint32_t mean_c, var_c, p2p_c;
    int8_t   rssi;
    uint32_t ts_ms;         /* 最新窗口时刻（节点本地 uptime，跨 observer 不可比） */
    uint32_t age_ms;        /* S3 本地时钟计的更新龄 */
    uint32_t seq_lost;      /* 该链路 observer 的 ESP-NOW seq 断流累计 */
} cis_mesh_link_info_t;

bool cis_mesh_link_get(size_t i, cis_mesh_link_info_t *out);

/** 发送方节点统计（`mesh status` 用）。i ∈ [0,4)：0=经典#1 / 1=C3 / 2=经典#2 / 3=S3。
 *  无数据时 seen 返回 false。 */
bool cis_mesh_node_get(size_t i, uint8_t *observer, bool *seen,
                       uint32_t *age_ms, uint32_t *pkts, uint32_t *lost);

/** mesh 计数一行摘要（espnow 收发/丢包），`mesh status` / `dump` 用 */
void cis_mesh_status(char *out, size_t len);

#ifdef __cplusplus
}
#endif
