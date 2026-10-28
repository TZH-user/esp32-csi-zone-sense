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
#define CIS_FT_LOG             0x20
#define CIS_FT_TRAIN_RESULT    0x30

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

_Static_assert(sizeof(cis_payload_heartbeat_t) == 11, "heartbeat payload must be 11B");
_Static_assert(sizeof(cis_payload_event_t)     == 15, "event payload must be 15B");
_Static_assert(sizeof(cis_payload_feature_t)   == 17, "feature payload must be 17B");
_Static_assert(sizeof(cis_payload_train_t)     == 12, "train payload must be 12B");

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
