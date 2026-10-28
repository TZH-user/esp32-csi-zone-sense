/*
 * csi_tx_v1 · cis_tx_train.c — per-source 静默标定实现（见 cis_tx_train.h 头注释）
 *
 * 数据面：cis_csi_rx 排窗回调（esp_timer 任务）→ 本模块累计 var_c；
 * 控制面：cis_now 打包任务 cis_tx_train_poll_done → 弹出完成项 + NVS 落盘 +
 * 回 CALIB_RSP。训练状态只被两个上下文触碰（hook 写累计、poll 读/清），
 * 全部经 s_mux 短临界区保护。
 */
#include <string.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs.h"
#include "esp_mac.h"

#include "cis_csi_rx.h"
#include "cis_tx_train.h"

static const char *TAG = "cis_tx_train";

/* 持久化 blob（magic 校验防陈旧/残缺数据） */
typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t threshold_c;
    uint16_t noise_c;
    uint16_t train_s;
} trx_blob_t;

#define TRX_BLOB_MAGIC  0x43545258u   /* "TRX" */

typedef struct {
    uint8_t  mac[6];
    bool     used;
    uint16_t threshold_c;       /* 当前生效阈值（centi-var） */
    /* 训练中状态 */
    bool     training;
    uint16_t train_total_s;
    uint32_t train_start_ms;
    uint32_t train_last_log_ms;
    uint64_t train_var_acc;     /* Σ窗方差（centi-var） */
    uint32_t train_windows;
} trx_ctx_t;

#define TRX_SLOTS  CIS_CSI_SRC_MAX

static trx_ctx_t s_ctx[TRX_SLOTS];
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

/* 完成就绪队列（训练在 timer 任务完成，弹取在打包任务） */
static cis_tx_train_result_t s_done[TRX_SLOTS];
static uint32_t s_done_head, s_done_tail;

static inline uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000ULL);
}

static trx_ctx_t *ctx_find_nolock(const uint8_t mac[6])
{
    for (int i = 0; i < TRX_SLOTS; i++) {
        if (s_ctx[i].used && memcmp(s_ctx[i].mac, mac, 6) == 0) {
            return &s_ctx[i];
        }
    }
    return NULL;
}

static void mac_to_key(const uint8_t mac[6], char key[13])
{
    snprintf(key, 13, "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

/* ---- NVS 持久化（打包任务上下文调用） ---- */

static void threshold_save(const uint8_t mac[6], uint16_t threshold_c,
                           uint16_t noise_c, uint16_t train_s)
{
    nvs_handle_t h;
    if (nvs_open(CIS_TX_TRAIN_NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE(TAG, "nvs_open " CIS_TX_TRAIN_NVS_NS " failed");
        return;
    }
    char key[13];
    mac_to_key(mac, key);
    trx_blob_t blob = {
        .magic = TRX_BLOB_MAGIC,
        .threshold_c = threshold_c,
        .noise_c = noise_c,
        .train_s = train_s,
    };
    esp_err_t err = nvs_set_blob(h, key, &blob, sizeof(blob));
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "threshold save " MACSTR " failed: %s", MAC2STR(mac), esp_err_to_name(err));
    }
}

static bool threshold_load(const uint8_t mac[6], uint16_t *threshold_c)
{
    nvs_handle_t h;
    if (nvs_open(CIS_TX_TRAIN_NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    char key[13];
    mac_to_key(mac, key);
    trx_blob_t blob;
    size_t len = sizeof(blob);
    bool ok = (nvs_get_blob(h, key, &blob, &len) == ESP_OK &&
               len == sizeof(blob) && blob.magic == TRX_BLOB_MAGIC);
    nvs_close(h);
    if (ok) {
        *threshold_c = blob.threshold_c;
    }
    return ok;
}

/* ---- 排窗回调（esp_timer 任务上下文，轻量：只累计数值） ---- */

static void train_window_hook(const cis_csi_feat_t *feat)
{
    portENTER_CRITICAL(&s_mux);
    trx_ctx_t *d = ctx_find_nolock(feat->mac);
    if (!d || !d->training) {
        portEXIT_CRITICAL(&s_mux);
        return;
    }
    d->train_var_acc += feat->var_c;
    d->train_windows++;
    uint32_t start = d->train_start_ms;
    uint32_t last_log = d->train_last_log_ms;
    uint32_t total_s = d->train_total_s;
    uint32_t windows = d->train_windows;
    uint64_t var_acc = d->train_var_acc;
    portEXIT_CRITICAL(&s_mux);

    uint32_t elapsed = (now_ms() - start) / 1000;
    if (now_ms() - last_log >= 1000) {
        portENTER_CRITICAL(&s_mux);
        if (d->training) {
            d->train_last_log_ms = now_ms();
        }
        portEXIT_CRITICAL(&s_mux);
        uint32_t remain = elapsed >= total_s ? 0 : total_s - elapsed;
        ESP_LOGI(TAG, "training " MACSTR ": %us left, windows=%u, mean_var=%u c",
                 MAC2STR(feat->mac), (unsigned)remain, (unsigned)windows,
                 (unsigned)(windows ? (uint32_t)(var_acc / windows) : 0));
    }
    if (elapsed < total_s) {
        return;
    }

    /* 完成：噪声底 = 窗方差均值 → 阈值 = 噪声底 × margin%（下限钳制）
     * （逻辑对照 cis_detect.c:293-300）；NVS 与 CALIB_RSP 延迟到打包任务。 */
    uint16_t noise = windows ? (uint16_t)((var_acc / windows) > 0xFFFFu
                                          ? 0xFFFFu : (var_acc / windows))
                             : CIS_TX_TRAIN_MIN_THRESHOLD;
    uint32_t thr = (uint32_t)noise * CIS_TX_TRAIN_MARGIN_PCT / 100;
    if (thr < CIS_TX_TRAIN_MIN_THRESHOLD) {
        thr = CIS_TX_TRAIN_MIN_THRESHOLD;
    }
    uint16_t threshold = (thr > 0xFFFFu) ? 0xFFFFu : (uint16_t)thr;

    portENTER_CRITICAL(&s_mux);
    if (d->training) {           /* 可能在临界区外被再次 start，防御性复查 */
        d->training = false;
        d->threshold_c = threshold;
        cis_tx_train_result_t *r = &s_done[s_done_head % TRX_SLOTS];
        memcpy(r->mac, feat->mac, 6);
        r->train_s = (uint16_t)total_s;
        r->threshold_c = threshold;
        r->noise_c = noise;
        s_done_head++;
    }
    portEXIT_CRITICAL(&s_mux);
    ESP_LOGI(TAG, "train done " MACSTR ": noise=%u c, threshold=%u c (%u windows) — NVS 在打包任务落盘",
             MAC2STR(feat->mac), noise, threshold, (unsigned)windows);
}

/* ---- 管理接口 ---- */

esp_err_t cis_tx_train_init(void)
{
    memset(s_ctx, 0, sizeof(s_ctx));
    s_done_head = s_done_tail = 0;

    uint8_t srcs[CIS_CSI_SRC_MAX][6];
    int n = cis_csi_rx_sources(srcs, CIS_CSI_SRC_MAX);
    for (int i = 0; i < n && i < TRX_SLOTS; i++) {
        s_ctx[i].used = true;
        memcpy(s_ctx[i].mac, srcs[i], 6);
        uint16_t thr;
        if (threshold_load(srcs[i], &thr)) {
            s_ctx[i].threshold_c = thr;
            ESP_LOGI(TAG, "restored " MACSTR " threshold=%u c (NVS " CIS_TX_TRAIN_NVS_NS ")",
                     MAC2STR(srcs[i]), thr);
        }
    }
    cis_csi_rx_set_window_hook(&train_window_hook);
    ESP_LOGI(TAG, "ready: train=%us margin=%u%% min_thr=%u c (per-source, NVS persisted)",
             CIS_TX_TRAIN_DEFAULT_S, CIS_TX_TRAIN_MARGIN_PCT, CIS_TX_TRAIN_MIN_THRESHOLD);
    return ESP_OK;
}

esp_err_t cis_tx_train_start(const uint8_t mac[6], uint16_t seconds)
{
    if (!mac) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_mux);
    trx_ctx_t *d = ctx_find_nolock(mac);
    if (!d) {
        portEXIT_CRITICAL(&s_mux);
        return ESP_ERR_NOT_FOUND;      /* 非白名单源 */
    }
    if (d->training) {
        portEXIT_CRITICAL(&s_mux);
        return ESP_ERR_INVALID_STATE;
    }
    d->training = true;
    d->train_total_s = seconds ? seconds : (uint16_t)CIS_TX_TRAIN_DEFAULT_S;
    d->train_start_ms = now_ms();
    d->train_last_log_ms = d->train_start_ms;
    d->train_var_acc = 0;
    d->train_windows = 0;
    portEXIT_CRITICAL(&s_mux);
    ESP_LOGI(TAG, "train start " MACSTR " for %us — 确保清场、无人静止！",
             MAC2STR(mac), seconds);
    return ESP_OK;
}

bool cis_tx_train_poll_done(cis_tx_train_result_t *out)
{
    if (!out) {
        return false;
    }
    bool have = false;
    portENTER_CRITICAL(&s_mux);
    if (s_done_tail != s_done_head) {
        *out = s_done[s_done_tail % TRX_SLOTS];
        s_done_tail++;
        have = true;
    }
    portEXIT_CRITICAL(&s_mux);
    if (have) {
        threshold_save(out->mac, out->threshold_c, out->noise_c, out->train_s);
        ESP_LOGI(TAG, "0x02 CALIB_RSP pending " MACSTR ": thr=%u noise=%u (NVS saved)",
                 MAC2STR(out->mac), out->threshold_c, out->noise_c);
    }
    return have;
}

uint16_t cis_tx_train_get_threshold(const uint8_t mac[6])
{
    if (!mac) {
        return 0;
    }
    portENTER_CRITICAL(&s_mux);
    trx_ctx_t *d = ctx_find_nolock(mac);
    uint16_t thr = d ? d->threshold_c : 0;
    portEXIT_CRITICAL(&s_mux);
    return thr;
}
