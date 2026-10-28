/*
 * ESP32_CIS_V1.0 · cis_report.c — 二进制帧上报
 *
 * 通道：串口主通道 = console 所在 USB-Serial-JTAG（COM3，PC 侧 2M），直写
 * usb_serial_jtag_write_bytes（绕过 VFS，避免控制台 CRLF 翻译破坏二进制帧）；
 * UDP 可选（默认关，端口 3333，同帧格式，目标默认 255.255.255.255 广播）。
 * 控制台日志与帧在串口上字节交织是设计预期（实施计划 §5：PC 端 0xAA55 搜帧 +
 * CRC 校验 + 失步重同步）。
 *
 * 多节点升级（2026-10）：track 键 = (observer, mac) 7 字节，本地雷达链路
 * observer=CIS_OBSERVER_SELF，远端链路（cis_mesh 汇聚）observer=发送方 node_id，
 * 共 12 链路统一发 0x12/0x13/0x14；CIS_LEGACY_FRAMES=y 时本地链路同时补发
 * 遗留 0x10/0x11/0x30（回归对照）。
 */
#include <string.h>
#include <stdio.h>
#include <stdarg.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "driver/usb_serial_jtag.h"
#include "lwip/sockets.h"
#include "nvs.h"

#include "cis_report.h"
#include "cis_protocol.h"
#include "cis_peer.h"
#include "cis_detect.h"

static const char *TAG = "cis_report";

/* ---- 内部消息 ---- */
typedef enum {
    MSG_FEATURE = 0,
    MSG_EVENT,
    MSG_TRAIN,
    MSG_LOG,
} msg_type_t;

typedef struct {
    msg_type_t type;
    uint8_t  observer;          /* 发送方源 MAC[5]（链路 key 的一部分） */
    uint8_t  mac[6];
    uint8_t  state;
    int8_t   rssi;
    uint16_t a, b, c;           /* feature: mean/var/p2p ; event: score ; train: s/thr/noise */
    uint32_t ts_ms;
    char     text[96];          /* MSG_LOG */
} report_msg_t;

/* 12 链路 × 10Hz 特征 + 事件突发 + 日志；64 时事件突发会挤掉特征候选（多节点扩容） */
#define MSG_QUEUE_LEN  128

/* 遗留帧补发开关（回归对照）：仅对本地链路（observer=self）补发 0x10/0x11/0x30 */
#ifdef CONFIG_CIS_LEGACY_FRAMES
static inline bool legacy_self(uint8_t observer)
{
    return observer == CIS_OBSERVER_SELF;
}
#else
static inline bool legacy_self(uint8_t observer)
{
    (void)observer;
    return false;
}
#endif

static QueueHandle_t s_queue;
static bool s_enable = true;
static uint8_t s_rate = CONFIG_CIS_FEATURE_RATE_HZ;

/* UDP（bool Kconfig 关闭时宏不存在，须用 #ifdef） */
#ifdef CONFIG_CIS_UDP_REPORT_DEFAULT
static bool s_udp_en = true;
#else
static bool s_udp_en = false;
#endif
static int s_udp_sock = -1;
static char s_udp_ip[46] = "255.255.255.255";
static uint32_t s_udp_target = 0;   /* network order；inet_aton 失败时 0 */
#define CIS_UDP_PORT CONFIG_CIS_UDP_REPORT_PORT

static void udp_refresh_target(void)
{
    struct in_addr addr;
    if (inet_aton(s_udp_ip, &addr)) {
        s_udp_target = addr.s_addr;
    } else {
        s_udp_target = 0;
    }
}

/* 每条链路的最新特征 + 发送节拍（key = (observer, mac)，本地+远端共 12 条） */
typedef struct {
    uint8_t  observer;
    uint8_t  mac[6];
    bool     used;
    uint16_t mean_c, var_c, p2p_c;
    int8_t   rssi;
    uint32_t ts_ms;
    uint32_t last_sent_ms;
    /* 事件持续重发 */
    uint8_t  last_state;
    uint16_t last_score;
    uint32_t last_event_ms;
} feat_track_t;

#define FEAT_TRACK_MAX  CIS_LINK_MAX     /* 4 observer × 3 source = 12（原 CIS_PEER_MAX=8） */
static feat_track_t s_track[FEAT_TRACK_MAX];

static uint32_t s_dropped_queue, s_dropped_uart;

static inline uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000ULL);
}

/* ---- 帧发送 ---- */

esp_err_t cis_report_send_frame(uint8_t type, const void *payload, size_t len)
{
    static portMUX_TYPE send_mux = portMUX_INITIALIZER_UNLOCKED;
    static uint8_t frame[CIS_FRAME_OVERHEAD + 128];

    if (len > 128) {
        return ESP_ERR_INVALID_SIZE;
    }

    portENTER_CRITICAL(&send_mux);
    frame[0] = CIS_FRAME_SYNC0;
    frame[1] = CIS_FRAME_SYNC1;
    frame[2] = CIS_PROTO_VER;
    frame[3] = type;
    frame[4] = (uint8_t)(len & 0xFF);
    frame[5] = (uint8_t)(len >> 8);
    if (len) {
        memcpy(frame + 6, payload, len);
    }
    uint16_t crc = cis_crc16_ccitt(frame + 2, len + 4);   /* ver..payload */
    frame[6 + len] = (uint8_t)(crc & 0xFF);
    frame[7 + len] = (uint8_t)(crc >> 8);
    size_t total = len + CIS_FRAME_OVERHEAD;
    portEXIT_CRITICAL(&send_mux);

    esp_err_t err = ESP_OK;
    if (s_enable) {
        int written = usb_serial_jtag_write_bytes(frame, total, pdMS_TO_TICKS(20));
        if (written < (int)total) {
            s_dropped_uart++;
            err = ESP_ERR_INVALID_STATE;
        }
    }

    if (s_udp_en && s_udp_sock >= 0 && s_udp_target) {
        struct sockaddr_in dst = {
            .sin_family = AF_INET,
            .sin_port = htons(CIS_UDP_PORT),
            .sin_addr.s_addr = s_udp_target,
        };
        sendto(s_udp_sock, frame, total, 0, (struct sockaddr *)&dst, sizeof(dst));
    }
    return err;
}

/* ---- 消息入队 ---- */

static void push_msg(const report_msg_t *msg)
{
    if (!s_queue) {
        return;
    }
    if (xQueueSend(s_queue, msg, 0) != pdTRUE) {
        s_dropped_queue++;
    }
}

void cis_report_feature_ex(uint8_t observer, const uint8_t mac[6], uint32_t ts_ms,
                           uint16_t mean_c, uint16_t var_c, uint16_t p2p_c, int8_t rssi)
{
    report_msg_t m = {
        .type = MSG_FEATURE,
        .observer = observer,
        .rssi = rssi,
        .a = mean_c, .b = var_c, .c = p2p_c,
        .ts_ms = ts_ms,
    };
    memcpy(m.mac, mac, 6);
    push_msg(&m);
}

void cis_report_feature(const uint8_t mac[6], uint32_t ts_ms,
                        uint16_t mean_c, uint16_t var_c, uint16_t p2p_c, int8_t rssi)
{
    cis_report_feature_ex(CIS_OBSERVER_SELF, mac, ts_ms, mean_c, var_c, p2p_c, rssi);
}

void cis_report_event_ex(uint8_t observer, const uint8_t mac[6], uint8_t state,
                         uint16_t score_c, int8_t rssi, uint32_t ts_ms)
{
    report_msg_t m = {
        .type = MSG_EVENT,
        .observer = observer,
        .state = state,
        .rssi = rssi,
        .a = score_c,
        .ts_ms = ts_ms,
    };
    memcpy(m.mac, mac, 6);
    push_msg(&m);
}

void cis_report_event(const uint8_t mac[6], uint8_t state, uint16_t score_c,
                      int8_t rssi, uint32_t ts_ms)
{
    cis_report_event_ex(CIS_OBSERVER_SELF, mac, state, score_c, rssi, ts_ms);
}

void cis_report_train_result_ex(uint8_t observer, const uint8_t mac[6], uint16_t train_s,
                                uint16_t threshold_c, uint16_t noise_c)
{
    report_msg_t m = {
        .type = MSG_TRAIN,
        .observer = observer,
        .a = train_s, .b = threshold_c, .c = noise_c,
    };
    memcpy(m.mac, mac, 6);
    push_msg(&m);
}

void cis_report_train_result(const uint8_t mac[6], uint16_t train_s,
                             uint16_t threshold_c, uint16_t noise_c)
{
    cis_report_train_result_ex(CIS_OBSERVER_SELF, mac, train_s, threshold_c, noise_c);
}

void cis_report_log(const char *fmt, ...)
{
    report_msg_t m = { .type = MSG_LOG, .ts_ms = now_ms() };
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(m.text, sizeof(m.text), fmt, ap);
    va_end(ap);
    push_msg(&m);
}

/* ---- 心跳 ---- */

static void send_heartbeat(void)
{
    cis_payload_heartbeat_t hb = {
        .uptime_ms = now_ms(),
        .peer_count = cis_peer_count(),
        .free_heap = esp_get_free_heap_size(),
        .lost_frames = (uint16_t)((cis_detect_lost_frames() + s_dropped_queue + s_dropped_uart) & 0xFFFF),
    };
    cis_report_send_frame(CIS_FT_HEARTBEAT, &hb, sizeof(hb));
}

/* ---- 任务 ---- */

static feat_track_t *track_find(uint8_t observer, const uint8_t mac[6], bool create)
{
    feat_track_t *free_slot = NULL;
    for (int i = 0; i < FEAT_TRACK_MAX; i++) {
        if (s_track[i].used) {
            if (s_track[i].observer == observer && memcmp(s_track[i].mac, mac, 6) == 0) {
                return &s_track[i];
            }
        } else if (!free_slot) {
            free_slot = &s_track[i];
        }
    }
    if (create && free_slot) {
        memset(free_slot, 0, sizeof(*free_slot));
        free_slot->observer = observer;
        memcpy(free_slot->mac, mac, 6);
        free_slot->used = true;
        return free_slot;
    }
    return NULL;
}

static void handle_train(const report_msg_t *m)
{
    if (m->observer == CIS_OBSERVER_SELF) {
        /* 本地链路：阈值入 peer 表并持久化（远端链路阈值由 cis_mesh 回填链路表 + cis_link） */
        cis_peer_set_threshold(m->mac, m->b);
        cis_peer_save();
    }

    /* 0x14 标定·多节点（全部 12 链路统一） */
    cis_payload_train_multi_t tr = {
        .observer_id = m->observer,
        .train_s = m->a,
        .threshold = m->b,
        .noise_floor = m->c,
    };
    memcpy(tr.mac, m->mac, 6);
    cis_report_send_frame(CIS_FT_CALIB_MULTI, &tr, sizeof(tr));
    ESP_LOGI(TAG, "0x14 calib %u/" MACSTR ": thr=%u noise=%u",
             m->observer, MAC2STR(m->mac), m->b, m->c);

    /* 遗留 0x30（回归对照开关打开时，仅本地链路） */
    if (legacy_self(m->observer)) {
        cis_payload_train_t tr0 = {
            .train_s = m->a,
            .threshold = m->b,
            .noise_floor = m->c,
        };
        memcpy(tr0.mac, m->mac, 6);
        cis_report_send_frame(CIS_FT_TRAIN_RESULT, &tr0, sizeof(tr0));
    }
}

static void report_task(void *arg)
{
    (void)arg;
    report_msg_t m;
    uint32_t next_hb_ms = now_ms() + 1000;
    const uint32_t housekeeping_ms = 50;

    ESP_LOGI(TAG, "report task: uart=USB-Serial-JTAG(console) udp=%s:%u rate=%uHz",
             s_udp_en ? s_udp_ip : "off", CIS_UDP_PORT, s_rate);

    for (;;) {
        TickType_t wait = pdMS_TO_TICKS(housekeeping_ms);
        if (xQueueReceive(s_queue, &m, wait) == pdTRUE) {
            uint32_t ts = now_ms();
            switch (m.type) {
            case MSG_FEATURE: {
                feat_track_t *t = track_find(m.observer, m.mac, true);
                if (t) {
                    t->mean_c = m.a;
                    t->var_c = m.b;
                    t->p2p_c = m.c;
                    t->rssi = m.rssi;
                    t->ts_ms = m.ts_ms;
                }
                break;
            }
            case MSG_EVENT: {
                feat_track_t *t = track_find(m.observer, m.mac, true);
                if (t) {
                    t->last_state = m.state;
                    t->last_score = m.a;
                    t->last_event_ms = ts;
                }
                int name_id = cis_peer_find(m.mac);   /* name_id 仍按 source mac 查表 */
                cis_payload_event_multi_t ev = {
                    .observer_id = m.observer,
                    .name_id = (uint8_t)(name_id < 0 ? 0xFF : name_id),
                    .state = m.state,
                    .score = m.a,
                    .rssi = m.rssi,
                    .ts_ms = m.ts_ms,
                };
                memcpy(ev.mac, m.mac, 6);
                cis_report_send_frame(CIS_FT_EVENT_MULTI, &ev, sizeof(ev));
                ESP_LOGI(TAG, "0x13 event %u/" MACSTR " name_id=%d state=%u score=%u",
                         m.observer, MAC2STR(m.mac), ev.name_id, m.state, m.a);
                /* 遗留 0x10（回归对照开关打开时，仅本地链路） */
                if (legacy_self(m.observer)) {
                    cis_payload_event_t ev0 = {
                        .name_id = (uint8_t)(name_id < 0 ? 0xFF : name_id),
                        .state = m.state,
                        .score = m.a,
                        .rssi = m.rssi,
                        .ts_ms = m.ts_ms,
                    };
                    memcpy(ev0.mac, m.mac, 6);
                    cis_report_send_frame(CIS_FT_EVENT, &ev0, sizeof(ev0));
                }
                break;
            }
            case MSG_TRAIN:
                handle_train(&m);
                break;
            case MSG_LOG: {
                cis_report_send_frame(CIS_FT_LOG, m.text, strnlen(m.text, sizeof(m.text)));
                ESP_LOGI(TAG, "log: %s", m.text);
                break;
            }
            default:
                break;
            }
        }

        uint32_t ts = now_ms();

        /* 0x01 心跳 1Hz */
        if ((int32_t)(ts - next_hb_ms) >= 0) {
            next_hb_ms = ts + 1000;
            send_heartbeat();
        }

        /* 0x12 特征·多节点节拍（每链路 rate Hz，最新值优先）+ MOTION 持续期 0x13 2Hz */
        if (s_enable) {
            uint32_t interval = 1000u / s_rate;
            for (int i = 0; i < FEAT_TRACK_MAX; i++) {
                feat_track_t *t = &s_track[i];
                if (!t->used || !t->ts_ms) {
                    continue;
                }
                if ((int32_t)(ts - t->last_sent_ms) >= (int32_t)interval && t->ts_ms) {
                    t->last_sent_ms = ts;
                    cis_payload_feature_multi_t f = {
                        .observer_id = t->observer,
                        .ts_ms = t->ts_ms,
                        .mean = t->mean_c,
                        .var = t->var_c,
                        .p2p = t->p2p_c,
                        .rssi = t->rssi,
                    };
                    memcpy(f.mac, t->mac, 6);
                    cis_report_send_frame(CIS_FT_FEATURE_MULTI, &f, sizeof(f));
                    /* 遗留 0x11（回归对照开关打开时，仅本地链路） */
                    if (legacy_self(t->observer)) {
                        cis_payload_feature_t f0 = {
                            .ts_ms = t->ts_ms,
                            .mean = t->mean_c,
                            .var = t->var_c,
                            .p2p = t->p2p_c,
                            .rssi = t->rssi,
                        };
                        memcpy(f0.mac, t->mac, 6);
                        cis_report_send_frame(CIS_FT_FEATURE, &f0, sizeof(f0));
                    }
                }
                /* MOTION 持续期重发（2Hz） */
                if (t->last_state == CIS_STATE_MOTION &&
                        (int32_t)(ts - t->last_event_ms) >= 500) {
                    t->last_event_ms = ts;
                    int name_id = cis_peer_find(t->mac);
                    cis_payload_event_multi_t ev = {
                        .observer_id = t->observer,
                        .name_id = (uint8_t)(name_id < 0 ? 0xFF : name_id),
                        .state = CIS_STATE_MOTION,
                        .score = t->last_score,
                        .rssi = t->rssi,
                        .ts_ms = ts,
                    };
                    memcpy(ev.mac, t->mac, 6);
                    cis_report_send_frame(CIS_FT_EVENT_MULTI, &ev, sizeof(ev));
                    /* 遗留 0x10（回归对照开关打开时，仅本地链路） */
                    if (legacy_self(t->observer)) {
                        cis_payload_event_t ev0 = {
                            .name_id = (uint8_t)(name_id < 0 ? 0xFF : name_id),
                            .state = CIS_STATE_MOTION,
                            .score = t->last_score,
                            .rssi = t->rssi,
                            .ts_ms = ts,
                        };
                        memcpy(ev0.mac, t->mac, 6);
                        cis_report_send_frame(CIS_FT_EVENT, &ev0, sizeof(ev0));
                    }
                }
            }
        }
    }
}

/* ---- 启动与命令接口 ---- */

esp_err_t cis_report_start(void)
{
    if (s_queue) {
        return ESP_OK;
    }
    memset(s_track, 0, sizeof(s_track));
    s_queue = xQueueCreate(MSG_QUEUE_LEN, sizeof(report_msg_t));
    if (!s_queue) {
        return ESP_ERR_NO_MEM;
    }
    udp_refresh_target();

    if (s_udp_en) {
        s_udp_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (s_udp_sock >= 0) {
            int broadcast = 1;
            setsockopt(s_udp_sock, SOL_SOCKET, SO_BROADCAST, &broadcast, sizeof(broadcast));
        }
    }

    if (xTaskCreate(report_task, "cis_report", 4096, NULL, 5, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void cis_report_set_enable(bool en)
{
    s_enable = en;
}

bool cis_report_get_enable(void)
{
    return s_enable;
}

void cis_report_set_rate(uint8_t hz)
{
    if (hz < 1) {
        hz = 1;
    }
    if (hz > 50) {
        hz = 50;
    }
    s_rate = hz;
}

uint8_t cis_report_get_rate(void)
{
    return s_rate;
}

esp_err_t cis_report_udp_set(bool en, const char *ip)
{
    s_udp_en = en;
    if (ip && ip[0]) {
        strlcpy(s_udp_ip, ip, sizeof(s_udp_ip));
        udp_refresh_target();
        if (s_udp_target == 0) {
            return ESP_ERR_INVALID_ARG;
        }
    }
    if (en && s_udp_sock < 0) {
        s_udp_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (s_udp_sock >= 0) {
            int broadcast = 1;
            setsockopt(s_udp_sock, SOL_SOCKET, SO_BROADCAST, &broadcast, sizeof(broadcast));
        }
    }
    return ESP_OK;
}

void cis_report_udp_status(char *out, size_t len)
{
    snprintf(out, len, "udp=%s target=%s:%d sock=%d", s_udp_en ? "on" : "off",
             s_udp_ip, CIS_UDP_PORT, s_udp_sock);
}

uint32_t cis_report_dropped(void)
{
    return s_dropped_queue + s_dropped_uart;
}
