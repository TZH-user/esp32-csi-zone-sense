/*
 * ESP32_CIS_V1.0 · cis_detect.c — per-peer 滑窗统计 + 滞回去抖 FSM（自管上下文）
 *
 * 与 esp_radar_motion_dec 的关系（实施计划 §4/§9.6）：只用 esp-radar 的数据通路
 * （csi_filtered_cb / wifi_radar_cb_ex），检测全部自管：per-peer 上下文、滑窗统计、
 * FSM、阈值与训练均为本模块私有状态，不触碰 esp_radar_motion_dec 的任何内部单例。
 *
 * 滑窗语义：窗口 = 两次 wifi_radar_cb_ex 之间的 CSI 包集合（esp-radar 检测窗，
 * 台架实测 ≈ 9~10Hz/peer，M1 数据 546 帧/60s）。每窗产出：
 *   mean  = 窗内逐包平均幅值的均值（centi-amp）
 *   var   = 窗内逐包平均幅值的方差（centi-var = raw² × 100）
 *   p2p   = 窗内逐包平均幅值峰峰值（centi-amp）
 * RSSI 突变过滤：|ΔRSSI|>6dB 的包不进滑窗且整窗标记 dirty 丢弃（AGC 增益跳变
 * 会让幅值分步跳变造成误报，esp-csi issue #185）。
 *
 * FSM（滞回去抖，M2 判据对齐）：
 *   IDLE --连续 N=CONFIG_CIS_MOTION_CONFIRM_FRAMES 窗超阈--> MOTION（发 0x13 state=1）
 *   MOTION --连续 M=CONFIG_CIS_STILL_CONFIRM_FRAMES 窗低--> PRESENCE（发 0x13 state=2，
 *            "停走 3s 内 EVENT STILL"：25 窗 @10Hz = 2.5s）
 *   PRESENCE --安静持续 CONFIG_CIS_PRESENCE_HOLD_S 秒--> IDLE（发 0x13 state=0）
 *   PRESENCE/MOTION --再次连续 N 窗超阈--> MOTION
 * 训练：每窗 var 累加 → 噪声底 = 均值（centi-var）→ 阈值 = 噪声底×margin（下限钳制）。
 *
 * 检测 key = (observer, mac) 7 字节（observer ≡ 发送方源 MAC[5]）：
 *   本地雷达回调固定 observer=CIS_OBSERVER_SELF（仅写 self 链路的 ctx）；
 *   远端链路经 cis_detect_feed_external（cis_mesh 任务调用，仅写远端 ctx）——
 *   每个 ctx 仍单写者，s_ctx_list 增删全部走 s_ctx_mux 临界区。
 */
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_mac.h"

#include "cis_detect.h"
#include "cis_peer.h"
#include "cis_report.h"
#include "cis_protocol.h"

static const char *TAG = "cis_detect";

/* ---------- per-MAC 逐包累加器（WiFi 回调上下文写，雷达回调读） ---------- */

typedef struct {
    uint8_t  mac[6];
    bool     used;
    int16_t  rssi_prev;         /* 上一包 RSSI（RSSI 突变过滤基准） */
    int16_t  rssi_last;         /* 最近一包 RSSI（窗口特征带出） */
    uint32_t acc_milli;         /* Σ单包平均幅值（毫单位） */
    uint64_t sq_acc_milli;      /* Σ(单包平均幅值毫单位)² */
    uint32_t cnt;               /* 窗内已累计包数 */
    uint32_t min_milli;
    uint32_t max_milli;
    bool     dirty;             /* 窗内出现 RSSI 突变 → 整窗丢弃 */
    uint32_t rssi_jump_drops;   /* 突变丢包计数 */
} csi_accum_t;

#define CSI_ACC_SLOTS 8

static csi_accum_t s_acc[CSI_ACC_SLOTS];
static portMUX_TYPE s_acc_mux = portMUX_INITIALIZER_UNLOCKED;

/* ---------- per-peer 检测上下文（仅雷达回调任务写 FSM，命令侧只读快照） ---------- */

typedef struct det_ctx {
    uint8_t  observer;         /* 发送方源 MAC[5]，key 的一部分 */
    uint8_t  mac[6];
    bool     used;
    uint8_t  state;
    uint16_t over_cnt;
    uint16_t under_cnt;
    uint32_t quiet_ms;          /* PRESENCE 期间累计安静时长 */
    uint32_t last_ts_ms;
    uint16_t threshold;         /* centi-var */
    uint32_t ema_mean_c;        /* 静息均值的 EMA（诊断用，centi-amp 刻度） */
    /* 训练 */
    bool     training;
    uint16_t train_total_s;
    uint32_t train_start_ms;
    uint32_t train_last_log_ms;
    uint64_t train_var_acc;     /* Σ窗方差（centi-var） */
    uint32_t train_windows;
    /* 最近窗口 */
    uint32_t mean_c, var_c, p2p_c;
    int8_t   rssi;
    uint32_t ts_ms;
    uint32_t windows;
    uint32_t dirty_windows;
    struct det_ctx *next;
} det_ctx_t;

static det_ctx_t *s_ctx_list = NULL;
static portMUX_TYPE s_ctx_mux = portMUX_INITIALIZER_UNLOCKED;
static bool s_started = false;

/* ---------- 小工具 ---------- */

static inline uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000ULL);
}

static inline uint16_t clamp_u16(uint32_t v)
{
    return (v > 0xFFFFu) ? 0xFFFFu : (uint16_t)v;
}

static det_ctx_t *ctx_find_nolock(uint8_t observer, const uint8_t mac[6])
{
    for (det_ctx_t *p = s_ctx_list; p; p = p->next) {
        if (p->observer == observer && memcmp(p->mac, mac, 6) == 0) {
            return p;
        }
    }
    return NULL;
}

/* 状态机对外动作：状态迁移 → 事件帧 */
static void fsm_transition(det_ctx_t *d, uint8_t new_state, uint32_t ts_ms)
{
    d->state = new_state;
    d->quiet_ms = 0;
    ESP_LOGI(TAG, "%u/" MACSTR " state=%u score=%u thr=%u rssi=%d",
             d->observer, MAC2STR(d->mac),
             new_state, d->var_c, d->threshold, d->rssi);
    cis_report_event_ex(d->observer, d->mac, new_state, clamp_u16(d->var_c), d->rssi, ts_ms);
}

/* ---------- esp-radar 逐包回调（WiFi 上下文，保持轻量） ---------- */

/* 单包平均幅值（毫单位）。valid_data 为 I/Q 交替（int8 或 int16），M1 同源算法 */
static uint32_t csi_mean_amp_milli(const wifi_csi_filtered_info_t *info)
{
    if (info->valid_len < 2) {
        return 0;
    }
    uint32_t acc = 0;
    size_t pairs = 0;
    if (info->data_type == WIFI_CSI_DATA_TYPE_INT16) {
        const int16_t *d = info->valid_data_i16;
        pairs = info->valid_len / (2 * sizeof(int16_t));
        for (size_t i = 0; i < pairs; i++) {
            acc += (uint32_t)sqrtf((float)d[2 * i] * d[2 * i] + (float)d[2 * i + 1] * d[2 * i + 1]);
        }
    } else {
        const int8_t *d = info->valid_data;
        pairs = info->valid_len / 2;
        for (size_t i = 0; i < pairs; i++) {
            acc += (uint32_t)sqrtf((float)d[2 * i] * d[2 * i] + (float)d[2 * i + 1] * d[2 * i + 1]);
        }
    }
    return pairs ? (uint32_t)(1000ULL * acc / pairs) : 0;
}

void cis_detect_csi_cb(void *ctx, const wifi_csi_filtered_info_t *info)
{
    (void)ctx;
    if (!info || info->valid_len < 2) {
        return;
    }
    uint32_t mean_milli = csi_mean_amp_milli(info);
    int rssi = info->rx_ctrl_info.rssi;

    cis_peer_note_csi(info->mac, rssi);   /* peer scan 观测表 */

    portENTER_CRITICAL(&s_acc_mux);
    csi_accum_t *slot = NULL;
    for (int i = 0; i < CSI_ACC_SLOTS; i++) {
        if (s_acc[i].used && memcmp(s_acc[i].mac, info->mac, 6) == 0) {
            slot = &s_acc[i];
            break;
        }
    }
    if (!slot) {
        for (int i = 0; i < CSI_ACC_SLOTS; i++) {
            if (!s_acc[i].used) {
                slot = &s_acc[i];
                memset(slot, 0, sizeof(*slot));
                slot->used = true;
                memcpy(slot->mac, info->mac, 6);
                slot->rssi_prev = rssi;
                break;
            }
        }
    }
    if (slot) {
        /* RSSI 突变过滤（|ΔRSSI|>6dB 不进滑窗） */
        if (abs(rssi - slot->rssi_prev) > 6) {
            slot->dirty = true;
            slot->rssi_jump_drops++;
        } else {
            if (slot->cnt == 0) {
                slot->min_milli = slot->max_milli = mean_milli;
            } else {
                if (mean_milli < slot->min_milli) {
                    slot->min_milli = mean_milli;
                }
                if (mean_milli > slot->max_milli) {
                    slot->max_milli = mean_milli;
                }
            }
            slot->acc_milli += mean_milli;
            slot->sq_acc_milli += (uint64_t)mean_milli * mean_milli;
            slot->cnt++;
        }
        slot->rssi_prev = (int16_t)rssi;
        slot->rssi_last = (int16_t)rssi;
    }
    portEXIT_CRITICAL(&s_acc_mux);
}

/* ---------- 窗口应用（本地雷达与远端 ESP-NOW 链路共用的 FSM 路径） ---------- */

/* 输入为一窗已换算 centi 刻度的有效统计。ctx 未注册（未 peer add / 链路未建）
 * 返回 ESP_ERR_NOT_FOUND，不检测。 */
esp_err_t cis_detect_apply_window(uint8_t observer, const uint8_t mac[6],
                                  uint32_t mean_c, uint32_t var_c, uint32_t p2p_c,
                                  int8_t rssi, uint32_t ts)
{
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }

    portENTER_CRITICAL(&s_ctx_mux);
    det_ctx_t *d = ctx_find_nolock(observer, mac);
    if (!d) {
        portEXIT_CRITICAL(&s_ctx_mux);
        return ESP_ERR_NOT_FOUND;   /* 未注册 peer → 不检测 */
    }
    d->windows++;

    d->mean_c = mean_c;
    d->var_c  = var_c;
    d->p2p_c  = p2p_c;
    d->rssi   = rssi;
    d->ts_ms = ts;
    d->last_ts_ms = ts;
    portEXIT_CRITICAL(&s_ctx_mux);

    cis_report_feature_ex(observer, mac, ts, clamp_u16(mean_c), clamp_u16(var_c),
                          clamp_u16(p2p_c), rssi);

    /* 训练收集 */
    if (d->training) {
        d->train_var_acc += d->var_c;
        d->train_windows++;
        uint32_t now = now_ms();   /* train_start_ms 为本地时钟，elapsed 必须用本地时钟 */
        uint32_t elapsed = (now - d->train_start_ms) / 1000;
        if (now - d->train_last_log_ms >= 1000) {
            d->train_last_log_ms = now;
            uint32_t remain = elapsed >= d->train_total_s ? 0 : d->train_total_s - elapsed;
            ESP_LOGI(TAG, "training %u/" MACSTR ": %us left, windows=%u, mean_var=%u c",
                     observer, MAC2STR(mac), (unsigned)remain, (unsigned)d->train_windows,
                     (unsigned)(d->train_windows ? d->train_var_acc / d->train_windows : 0));
        }
        if (elapsed >= d->train_total_s) {
            uint16_t noise = d->train_windows ? clamp_u16((uint32_t)(d->train_var_acc / d->train_windows))
                             : (uint16_t)CONFIG_CIS_TRAIN_MIN_THRESHOLD;
            uint32_t thr = (uint32_t)noise * CONFIG_CIS_TRAIN_MARGIN_PCT / 100;
            if (thr < CONFIG_CIS_TRAIN_MIN_THRESHOLD) {
                thr = CONFIG_CIS_TRAIN_MIN_THRESHOLD;
            }
            d->threshold = clamp_u16(thr);
            d->training = false;
            ESP_LOGI(TAG, "train done %u/" MACSTR ": noise=%u c, threshold=%u c (%u windows)",
                     observer, MAC2STR(mac), noise, d->threshold, (unsigned)d->train_windows);
            /* 0x14 帧 + NVS 持久化在 report 任务完成（避免高优先级雷达任务里写 NVS） */
            cis_report_train_result_ex(observer, mac, d->train_total_s, d->threshold, noise);
        }
        return ESP_OK;   /* 训练期间不做 FSM 判决，避免把标定过程当扰动 */
    }

    /* 滞回 FSM */
    uint16_t N = CONFIG_CIS_MOTION_CONFIRM_FRAMES;
    uint16_t M = CONFIG_CIS_STILL_CONFIRM_FRAMES;
    bool over = d->var_c > d->threshold;

    if (over) {
        d->over_cnt++;
        d->under_cnt = 0;
    } else {
        d->under_cnt++;
        d->over_cnt = 0;
        if (d->state != CIS_DET_IDLE) {
            d->quiet_ms += (ts > d->last_ts_ms) ? (ts - d->last_ts_ms) : 0;
        }
        /* 静息均值 EMA（诊断用；centi-amp 刻度） */
        d->ema_mean_c = d->ema_mean_c ? (d->ema_mean_c * 7 + mean_c) / 8 : mean_c;
    }

    switch (d->state) {
    case CIS_DET_IDLE:
        if (over && d->over_cnt >= N) {
            fsm_transition(d, CIS_DET_MOTION, ts);
        }
        break;
    case CIS_DET_MOTION:
        if (d->under_cnt >= M) {
            fsm_transition(d, CIS_DET_PRESENCE, ts);
        }
        break;
    case CIS_DET_PRESENCE:
        if (over && d->over_cnt >= N) {
            fsm_transition(d, CIS_DET_MOTION, ts);
        } else if (d->quiet_ms >= (uint32_t)CONFIG_CIS_PRESENCE_HOLD_S * 1000u) {
            fsm_transition(d, CIS_DET_IDLE, ts);
        }
        break;
    default:
        d->state = CIS_DET_IDLE;
        break;
    }
    d->last_ts_ms = ts;
    return ESP_OK;
}

esp_err_t cis_detect_feed_external(uint8_t observer, const uint8_t mac[6],
                                   uint32_t mean_c, uint32_t var_c, uint32_t p2p_c,
                                   int8_t rssi, uint32_t ts_ms)
{
    return cis_detect_apply_window(observer, mac, mean_c, var_c, p2p_c, rssi, ts_ms);
}

/* ---------- esp-radar 检测窗回调（雷达任务上下文，≈10Hz/peer） ---------- */

void cis_detect_radar_cb_ex(void *ctx, const uint8_t mac[6], const wifi_radar_info_t *info)
{
    (void)ctx;
    (void)info;
    if (!s_started) {
        return;
    }
    uint32_t ts = now_ms();

    /* 排出该 MAC 的窗口累加器 */
    uint32_t mean_milli = 0, var_milli2 = 0, p2p_milli = 0;
    int rssi_last = 0;
    bool have = false, dirty = false;

    portENTER_CRITICAL(&s_acc_mux);
    for (int i = 0; i < CSI_ACC_SLOTS; i++) {
        csi_accum_t *s = &s_acc[i];
        if (s->used && memcmp(s->mac, mac, 6) == 0) {
            if (s->cnt > 0) {
                mean_milli = s->acc_milli / s->cnt;
                uint64_t e2 = s->sq_acc_milli / s->cnt;
                var_milli2 = (e2 > (uint64_t)mean_milli * mean_milli)
                             ? (uint32_t)(e2 - (uint64_t)mean_milli * mean_milli) : 0;
                p2p_milli = s->max_milli - s->min_milli;
                have = true;
            }
            rssi_last = s->rssi_last;
            dirty = s->dirty;
            s->acc_milli = 0;
            s->sq_acc_milli = 0;
            s->cnt = 0;
            s->min_milli = 0;
            s->max_milli = 0;
            s->dirty = false;
            break;
        }
    }
    portEXIT_CRITICAL(&s_acc_mux);

    portENTER_CRITICAL(&s_ctx_mux);
    det_ctx_t *d = ctx_find_nolock(CIS_OBSERVER_SELF, mac);
    if (!d) {
        portEXIT_CRITICAL(&s_ctx_mux);
        return;   /* 未注册 peer（含 wildcard FF:FF..）→ 不检测 */
    }

    if (!have || dirty) {
        d->windows++;
        if (dirty) {
            d->dirty_windows++;
            ESP_LOGD(TAG, MACSTR " window dropped (RSSI jump)", MAC2STR(mac));
        }
        d->last_ts_ms = ts;
        portEXIT_CRITICAL(&s_ctx_mux);
        return;
    }
    portEXIT_CRITICAL(&s_ctx_mux);

    /* centi 刻度换算（见 cis_protocol.h 单位约定）→ 与远端链路同一路径跑 FSM */
    cis_detect_apply_window(CIS_OBSERVER_SELF, mac,
                            mean_milli / 10, var_milli2 / 10000, p2p_milli / 10,
                            (int8_t)rssi_last, ts);
}

/* ---------- 管理接口 ---------- */

void cis_detect_init(void)
{
    memset(s_acc, 0, sizeof(s_acc));
    s_ctx_list = NULL;
    s_started = false;
}

void cis_detect_start(void)
{
    s_started = true;
    ESP_LOGI(TAG, "started: N=%u M=%u presence_hold=%us train=%us margin=%u%%",
             CONFIG_CIS_MOTION_CONFIRM_FRAMES, CONFIG_CIS_STILL_CONFIRM_FRAMES,
             CONFIG_CIS_PRESENCE_HOLD_S, CONFIG_CIS_TRAIN_SECONDS,
             CONFIG_CIS_TRAIN_MARGIN_PCT);
}

esp_err_t cis_detect_peer_ensure(uint8_t observer, const uint8_t mac[6], uint16_t threshold_c)
{
    if (!mac) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_ctx_mux);
    det_ctx_t *d = ctx_find_nolock(observer, mac);
    bool created = false;
    if (!d) {
        portEXIT_CRITICAL(&s_ctx_mux);
        d = calloc(1, sizeof(det_ctx_t));
        if (!d) {
            return ESP_ERR_NO_MEM;
        }
        d->observer = observer;
        memcpy(d->mac, mac, 6);
        d->state = CIS_DET_IDLE;
        d->threshold = threshold_c ? threshold_c : (uint16_t)CONFIG_CIS_DEFAULT_THRESHOLD;
        created = true;
        portENTER_CRITICAL(&s_ctx_mux);
        det_ctx_t *race = ctx_find_nolock(observer, mac);
        if (race) {
            portEXIT_CRITICAL(&s_ctx_mux);
            free(d);
            d = race;
        } else {
            d->next = s_ctx_list;
            s_ctx_list = d;
            portEXIT_CRITICAL(&s_ctx_mux);
        }
    } else {
        if (threshold_c) {
            d->threshold = threshold_c;
        }
        portEXIT_CRITICAL(&s_ctx_mux);
    }
    ESP_LOGI(TAG, "%s peer ctx %u/" MACSTR " thr=%u c", created ? "created" : "updated",
             observer, MAC2STR(d->mac), d->threshold);
    return ESP_OK;
}

esp_err_t cis_detect_peer_remove(uint8_t observer, const uint8_t mac[6])
{
    portENTER_CRITICAL(&s_ctx_mux);
    det_ctx_t **pp = &s_ctx_list;
    while (*pp) {
        if ((*pp)->observer == observer && memcmp((*pp)->mac, mac, 6) == 0) {
            det_ctx_t *victim = *pp;
            *pp = victim->next;
            portEXIT_CRITICAL(&s_ctx_mux);
            free(victim);
            ESP_LOGI(TAG, "removed peer ctx %u/" MACSTR, observer, MAC2STR(mac));
            return ESP_OK;
        }
        pp = &(*pp)->next;
    }
    portEXIT_CRITICAL(&s_ctx_mux);
    return ESP_ERR_NOT_FOUND;
}

void cis_detect_set_threshold(uint8_t observer, const uint8_t mac[6], uint16_t threshold_c)
{
    portENTER_CRITICAL(&s_ctx_mux);
    det_ctx_t *d = ctx_find_nolock(observer, mac);
    if (d && threshold_c) {
        d->threshold = threshold_c;
    }
    portEXIT_CRITICAL(&s_ctx_mux);
}

esp_err_t cis_detect_train_start(uint8_t observer, const uint8_t mac[6], uint16_t seconds)
{
    portENTER_CRITICAL(&s_ctx_mux);
    det_ctx_t *d = ctx_find_nolock(observer, mac);
    if (!d) {
        portEXIT_CRITICAL(&s_ctx_mux);
        return ESP_ERR_NOT_FOUND;
    }
    if (d->training) {
        portEXIT_CRITICAL(&s_ctx_mux);
        return ESP_ERR_INVALID_STATE;
    }
    d->training = true;
    d->train_total_s = seconds ? seconds : (uint16_t)CONFIG_CIS_TRAIN_SECONDS;
    d->train_start_ms = now_ms();
    d->train_last_log_ms = d->train_start_ms;
    d->train_var_acc = 0;
    d->train_windows = 0;
    portEXIT_CRITICAL(&s_ctx_mux);
    ESP_LOGI(TAG, "train start %u/" MACSTR " for %us — 确保清场、无人静止！",
             observer, MAC2STR(mac), seconds);
    cis_report_log("train %u/%02x%02x%02x%02x%02x%02x start %us (keep area quiet)",
                   observer, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5],
                   (unsigned)d->train_total_s);
    return ESP_OK;
}

esp_err_t cis_detect_get_stats(uint8_t observer, const uint8_t mac[6], cis_det_stats_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_ctx_mux);
    det_ctx_t *d = ctx_find_nolock(observer, mac);
    if (!d) {
        portEXIT_CRITICAL(&s_ctx_mux);
        return ESP_ERR_NOT_FOUND;
    }
    memset(out, 0, sizeof(*out));
    out->observer = d->observer;
    memcpy(out->mac, d->mac, 6);
    out->state = d->state;
    out->threshold = d->threshold;
    out->training = d->training;
    out->train_total_s = d->train_total_s;
    if (d->training) {
        uint32_t elapsed = (now_ms() - d->train_start_ms) / 1000;
        out->train_left_s = elapsed >= d->train_total_s ? 0 : d->train_total_s - (uint16_t)elapsed;
    }
    out->mean_c = d->mean_c;
    out->var_c = d->var_c;
    out->p2p_c = d->p2p_c;
    out->rssi = d->rssi;
    out->ts_ms = d->ts_ms;
    out->windows = d->windows;
    out->dirty_windows = d->dirty_windows;
    portEXIT_CRITICAL(&s_ctx_mux);
    return ESP_OK;
}

void cis_detect_reset(void)
{
    portENTER_CRITICAL(&s_ctx_mux);
    for (det_ctx_t *p = s_ctx_list; p; p = p->next) {
        p->state = CIS_DET_IDLE;
        p->over_cnt = 0;
        p->under_cnt = 0;
        p->quiet_ms = 0;
    }
    portEXIT_CRITICAL(&s_ctx_mux);
    ESP_LOGW(TAG, "detect reset: all links -> IDLE (threshold/NVS untouched)");
}

uint32_t cis_detect_lost_frames(void)
{
    uint32_t drops = 0;
    portENTER_CRITICAL(&s_acc_mux);
    for (int i = 0; i < CSI_ACC_SLOTS; i++) {
        drops += s_acc[i].rssi_jump_drops;
    }
    portEXIT_CRITICAL(&s_acc_mux);
    return drops;
}
