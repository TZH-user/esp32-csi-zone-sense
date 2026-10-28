/*
 * csi_tx_v1 · cis_now.h — mesh ESP-NOW 上行/下行（"ef" 帧：esp-now frame）
 *
 * 与串口 0xAA55 帧族（RX 工程 cis_protocol.h，PC 唯一对照源）相互独立：
 * ef 帧只在节点间空口跑，S3 收到后转成 0x12/0x13/0x14 串口帧上报 PC。
 *
 * == ef 帧格式（头 8B + 记录）==
 *   C5 5A | ver(1B=0x01) | type(1B) | seq(2B LE) | node_id(1B) | nrec(1B)
 *   seq：每发送方滚动（S3 据此算链路丢失率）；node_id ≡ 发送方 CSI 源 MAC[5]
 *   （observer_id 同源）；无 CRC——802.11 FCS 之外保留 magic+seq 防串包即可。
 *
 * == 类型 ==
 *   0x01 FEAT_BUNDLE：nrec × 17B 记录（cis_ef_feat_rec_t）
 *       = source_mac(6) | ts_ms(4) | mean(2) | var(2) | p2p(2) | rssi(1)
 *         （与 0x12 去掉 observer_id 后同构，centi-amp/var 刻度同 0x11）
 *   0x02 CALIB_RSP  ：12B（cis_ef_calib_t）= mac(6)|train_s(2)|threshold(2)|noise(2)
 *   0x10 CMD_TRAIN  ：8B（cis_ef_cmd_train_t）= mac(6)|seconds(2)，S3→TX 单播
 *
 * == 容量 ==
 *   ESP-NOW 单包上限 250B → FEAT_BUNDLE nrec ≤ 14；本节点 3 条链路 @10Hz
 *   实发 8+3×17=59B/包。单播 S3（CONFIG_CIS_MESH_S3_MAC），不加密（与
 *   app_main.c 广播 peer encrypt=false 一致），channel=0 随 STA。
 *
 * == ts_ms 语义 ==
 *   各节点本地 uptime（esp_timer 毫秒），跨 observer 不可比；仅链路内单调、
 *   供 S3/PC 做同链路去重与到达排序（与 0x12 约定一致）。
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CIS_EF_SYNC0          0xC5
#define CIS_EF_SYNC1          0x5A
#define CIS_EF_VER            0x01

#define CIS_EFT_FEAT_BUNDLE   0x01
#define CIS_EFT_CALIB_RSP     0x02
#define CIS_EFT_CMD_TRAIN     0x10

/* ef 帧头（8B，字段顺序即线上顺序，全部小端） */
typedef struct __attribute__((packed)) {
    uint8_t  magic0;       /* 0xC5 */
    uint8_t  magic1;       /* 0x5A */
    uint8_t  ver;          /* CIS_EF_VER */
    uint8_t  type;         /* CIS_EFT_x */
    uint16_t seq;          /* 发送方滚动序号（LE） */
    uint8_t  node_id;      /* = 发送方源 MAC[5] */
    uint8_t  nrec;         /* FEAT_BUNDLE 记录条数；其余类型 0 */
} cis_ef_hdr_t;

/* FEAT_BUNDLE 记录（17B）：与 RX cis_payload_feature_t（0x11）字段同布局 */
typedef struct __attribute__((packed)) {
    uint8_t  mac[6];
    uint32_t ts_ms;
    uint16_t mean;         /* centi-amp */
    uint16_t var;          /* centi-var */
    uint16_t p2p;          /* centi-amp */
    int8_t   rssi;         /* dBm */
} cis_ef_feat_rec_t;

/* CALIB_RSP 载荷（12B）：与 RX cis_payload_train_t（0x30）字段同布局 */
typedef struct __attribute__((packed)) {
    uint8_t  mac[6];
    uint16_t train_s;
    uint16_t threshold;    /* centi-var */
    uint16_t noise_floor;  /* centi-var */
} cis_ef_calib_t;

/* CMD_TRAIN 载荷（8B）：S3→TX 单播，seconds=0 取固件默认时长 */
typedef struct __attribute__((packed)) {
    uint8_t  mac[6];
    uint16_t seconds;
} cis_ef_cmd_train_t;

_Static_assert(sizeof(cis_ef_hdr_t)        ==  8, "ef header must be 8B");
_Static_assert(sizeof(cis_ef_feat_rec_t)   == 17, "ef feature record must be 17B");
_Static_assert(sizeof(cis_ef_calib_t)      == 12, "ef calib payload must be 12B");
_Static_assert(sizeof(cis_ef_cmd_train_t)  ==  8, "ef cmd_train payload must be 8B");

/** 初始化：解析 CONFIG_CIS_MESH_S3_MAC 并注册单播 peer、注册 recv 回调
 *  （CMD_TRAIN 入队）、启动 10Hz 打包任务（聚合白名单链路最新窗口为
 *  FEAT_BUNDLE 单播 S3；轮询训练完成项 → NVS 落盘 + 回 CALIB_RSP）。
 *  须在 esp_now_init（app_main wifi_esp_now_init）之后调用。 */
esp_err_t cis_now_init(void);

#ifdef __cplusplus
}
#endif
