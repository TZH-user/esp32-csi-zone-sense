/*
 * ESP32_CIS_V1.0 · cis_protocol.h
 * 实施计划 §5 上报协议（二进制）—— 本文件是 PC 端 pc_dashboard/protocol.py 的唯一对照源。
 * 任何字段/长度/单位改动必须同步 PC 端。
 *
 * == 帧格式 ==
 *   0xAA 0x55 | ver(1B=0x01) | type(1B) | len(2B, LE) | payload(len B) | crc16(2B, LE)
 *   crc16-CCITT: poly=0x1021, init=0xFFFF, 无反射, 无终值异或（CCITT-FALSE），
 *                覆盖 ver 起（含）到 payload 末字节（不含 crc 本身）。
 *
 * == 单位约定（重要，PC 端解码必须一致）==
 *   amp_raw   = 单包 CSI 有效子载波平均幅值 mean(|I+jQ|)（M1 日志打印的 amp 值，
 *               台架静态实测 ≈ 37.93）。
 *   mean 字段 = amp_raw 窗均值 × 100（centi-amp，u16，>655.35 饱和为 0xFFFF）
 *   var 字段  = amp_raw 逐包窗方差（raw²）× 100，u16，>655.35 饱和
 *   p2p 字段  = amp_raw 窗内峰峰值（raw）× 100，u16，>655.35 饱和
 *   score / threshold / 噪声底 与 var 同刻度（raw² × 100）。
 *   rssi 字段 = int8 dBm。
 *   ts_ms / uptime_ms = esp_log_timestamp() 毫秒，u32 回绕 ~49.7 天。
 *
 * == 帧类型 ==
 *   0x01 心跳   : uptime_ms(4B) peer_count(1B) free_heap(4B) lost_frames(2B)   = 11B
 *   0x10 事件   : mac(6B) name_id(1B) state(1B) score(2B) rssi(1B) ts_ms(4B)  = 15B
 *                 state: 0=静止(IDLE) 1=运动(MOTION) 2=存在(PRESENCE)
 *                 score: 触发时窗方差（centi 刻度，与 threshold 直接比较）
 *                 发送时机：状态跳变即时 + MOTION 持续期 2Hz 重发
 *   0x11 特征流 : mac(6B) ts_ms(4B) mean(2B) var(2B) p2p(2B) rssi(1B)        = 17B
 *                 每 peer 默认 10Hz（CIS_FEATURE_RATE_HZ，`report rate 1~50` 可调）
 *   0x20 日志   : ASCII 文本（按需，当前用于 train 开始/结束提示）
 *   0x30 标定   : mac(6B) train_ms(2B) threshold(2B) noise_floor(2B)          = 12B
 *                 train_ms 为 `train` 命令设定时长（秒），threshold/noise_floor
 *                 为 centi-var 刻度；threshold 已同步写入 NVS。
 *   0x12 特征·多节点 : observer_id(1B) mac(6B) ts_ms(4B) mean(2B) var(2B) p2p(2B)
 *                      rssi(1B)                                              = 18B
 *   0x13 事件·多节点 : observer_id(1B) mac(6B) name_id(1B) state(1B) score(2B)
 *                      rssi(1B) ts_ms(4B)                                    = 16B
 *   0x14 标定·多节点 : observer_id(1B) mac(6B) train_s(2B) threshold(2B)
 *                      noise_floor(2B)                                       = 13B
 *
 * == observer_id 与多节点升级（v1.1 协议扩展，2026-10）==
 *   observer_id(1B) ≡ 发送方 CSI 源 MAC 末字节（MAC[5]）——零配置且与 PC 端
 *   MAC_DEVICE_MAP（按末字节映射）天然对齐：
 *     0x00 = 经典#1 TX1（csi_tx_v1, esp32, MAC 1a:00:00:00:00:00）
 *     0x01 = C3    TX2（csi_tx_c3_v1,   MAC 1a:00:00:00:00:01）
 *     0x02 = 经典#2 TX3（csi_tx_v1@node3, MAC 1a:00:00:00:00:02）
 *     0x03 = S3    RX （本固件, 自定义 STA MAC 1a:00:00:00:00:03）
 *   链路 = (observer_id, source mac) 共 4×3=12 条（CIS_LINK_MAX）；observer=S3 的
 *   3 条即本地链路。多节点特征经 ESP-NOW（cis_mesh）汇聚后，S3 对全部 12 条链路
 *   统一发 0x12/0x13/0x14；0x11/0x10/0x30 隐含 observer=self，转为遗留格式
 *   （PC 保留解析以兼容旧 CSV/回放；CIS_LEGACY_FRAMES=y 可补发做回归对照）。
 *   注意：0x12/0x13 的 ts_ms 是对应 observer 节点的本地 uptime，跨 observer
 *   不可比；PC 端只可做链路内单调与自身到达时间排序。
 *
 * == ver 为何保持 0x01 ==
 *   parser 对 ver 仅透传不硬校验（protocol.py ver passthrough），CRC 覆盖
 *   ver..payload 完整性不变，新类型号本身足以区分语义；0x02 留给未来对
 *   0x11 本身的不兼容改动。
 *
 * == 传输通道 ==
 *   串口 COM3 @ 2000000-8-N-1（USB-Serial-JTAG，与控制台日志字节交织，PC 端需
 *   0xAA55 搜帧 + CRC 校验 + 失步重同步）；UDP 可选（默认关，端口 3333），同帧格式。
 */
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <sys/cdefs.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CIS_FRAME_SYNC0        0xAA
#define CIS_FRAME_SYNC1        0x55
#define CIS_PROTO_VER          0x01

/* 帧类型 */
#define CIS_FT_HEARTBEAT       0x01
#define CIS_FT_EVENT           0x10
#define CIS_FT_FEATURE         0x11
#define CIS_FT_FEATURE_MULTI   0x12
#define CIS_FT_EVENT_MULTI     0x13
#define CIS_FT_CALIB_MULTI     0x14
#define CIS_FT_LOG             0x20
#define CIS_FT_TRAIN_RESULT    0x30

/* 多节点链路数：4 个 observer × 各 3 个源（observer≠source）= 12 条 */
#define CIS_LINK_MAX           12

/* 本节点 observer_id（≡ 本节点 CSI 源 MAC[5]，见头注释；Kconfig CIS_NODE_SELF_ID） */
#ifndef CIS_OBSERVER_SELF
#define CIS_OBSERVER_SELF      CONFIG_CIS_NODE_SELF_ID
#endif

/* 本节点 CSI 源 MAC（本地管理地址，末字节 = observer_id；机制同 TX 板
 * csi_tx_v1/main/app_main.c 的 esp_wifi_set_mac，cis_mesh 启动时设置到 STA） */
static inline void cis_self_source_mac(uint8_t mac[6])
{
    mac[0] = 0x1a;
    mac[1] = 0x00;
    mac[2] = 0x00;
    mac[3] = 0x00;
    mac[4] = 0x00;
    mac[5] = (uint8_t)CIS_OBSERVER_SELF;
}

/* 链路槽位映射（稳定，供 mesh 链路表 / cis_link NVS blob 索引）：
 * observer、source 末字节均限 0..3 且互异，slot = observer*3 + src'，
 * src' = src - (src > observer ? 1 : 0)，覆盖 0..11。无效组合返回 -1。 */
static inline int cis_link_slot(uint8_t observer, uint8_t src_last)
{
    if (observer > 3 || src_last > 3 || observer == src_last) {
        return -1;
    }
    return observer * 3 + (src_last > observer ? src_last - 1 : src_last);
}

/* 事件状态（0x10 state 字段） */
#define CIS_STATE_IDLE         0    /* 静止 */
#define CIS_STATE_MOTION       1    /* 运动 */
#define CIS_STATE_PRESENCE     2    /* 存在（刚停但人可能还在） */

/* 帧头/帧尾固定开销：sync(2)+ver(1)+type(1)+len(2)+crc(2) */
#define CIS_FRAME_OVERHEAD     8

/* 默认端口 */
#define CIS_UDP_REPORT_PORT_DEFAULT   3333
#define CIS_TCP_CONSOLE_PORT_DEFAULT  2323

/* ---- payload 结构（packed，字段顺序即线上顺序，全部小端） ---- */

typedef struct __attribute__((packed)) {
    uint32_t uptime_ms;
    uint8_t  peer_count;
    uint32_t free_heap;
    uint16_t lost_frames;
} cis_payload_heartbeat_t;

typedef struct __attribute__((packed)) {
    uint8_t  mac[6];
    uint8_t  name_id;
    uint8_t  state;          /* CIS_STATE_x */
    uint16_t score;          /* centi-var 刻度 */
    int8_t   rssi;           /* dBm */
    uint32_t ts_ms;
} cis_payload_event_t;

typedef struct __attribute__((packed)) {
    uint8_t  mac[6];
    uint32_t ts_ms;
    uint16_t mean;           /* centi-amp */
    uint16_t var;            /* centi-var */
    uint16_t p2p;            /* centi-amp */
    int8_t   rssi;           /* dBm */
} cis_payload_feature_t;

typedef struct __attribute__((packed)) {
    uint8_t  mac[6];
    uint16_t train_s;        /* 训练时长（秒） */
    uint16_t threshold;      /* centi-var */
    uint16_t noise_floor;    /* centi-var */
} cis_payload_train_t;

/* ---- 多节点帧（0x12/0x13/0x14）：在 0x11/0x10/0x30 基础上去掉 observer 隐含
 * （=self）语义，显式携带 observer_id；单位刻度与 0x11 完全一致 ---- */

typedef struct __attribute__((packed)) {
    uint8_t  observer_id;
    uint8_t  mac[6];
    uint32_t ts_ms;
    uint16_t mean;           /* centi-amp */
    uint16_t var;            /* centi-var */
    uint16_t p2p;            /* centi-amp */
    int8_t   rssi;           /* dBm */
} cis_payload_feature_multi_t;

typedef struct __attribute__((packed)) {
    uint8_t  observer_id;
    uint8_t  mac[6];
    uint8_t  name_id;
    uint8_t  state;          /* CIS_STATE_x */
    uint16_t score;          /* centi-var 刻度 */
    int8_t   rssi;           /* dBm */
    uint32_t ts_ms;
} cis_payload_event_multi_t;

typedef struct __attribute__((packed)) {
    uint8_t  observer_id;
    uint8_t  mac[6];
    uint16_t train_s;        /* 训练时长（秒） */
    uint16_t threshold;      /* centi-var */
    uint16_t noise_floor;    /* centi-var */
} cis_payload_train_multi_t;

_Static_assert(sizeof(cis_payload_heartbeat_t) == 11, "heartbeat payload must be 11B");
_Static_assert(sizeof(cis_payload_event_t)     == 15, "event payload must be 15B");
_Static_assert(sizeof(cis_payload_feature_t)   == 17, "feature payload must be 17B");
_Static_assert(sizeof(cis_payload_train_t)     == 12, "train payload must be 12B");
_Static_assert(sizeof(cis_payload_feature_multi_t) == 18, "feature multi payload must be 18B");
_Static_assert(sizeof(cis_payload_event_multi_t)   == 16, "event multi payload must be 16B");
_Static_assert(sizeof(cis_payload_train_multi_t)   == 13, "train multi payload must be 13B");

/* ---- 编解码助手 ---- */

/* u16 带饱和缩放：v = raw * scale，超过 0xFFFF 取 0xFFFF */
static inline uint16_t cis_scale_u16(uint32_t value, uint32_t scale)
{
    uint64_t v = (uint64_t)value * scale;
    return (v > 0xFFFFULL) ? 0xFFFFu : (uint16_t)v;
}

/* CRC16-CCITT-FALSE: poly 0x1021, init 0xFFFF */
static inline uint16_t cis_crc16_ccitt(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)((uint16_t)data[i] << 8);
        for (int b = 0; b < 8; b++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

#ifdef __cplusplus
}
#endif
