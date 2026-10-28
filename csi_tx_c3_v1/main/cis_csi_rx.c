/*
 * csi_tx_c3_v1 · cis_csi_rx.c — TX 节点本地 CSI 观测实现（见 cis_csi_rx.h 头注释）
 * 同步源 = csi_tx_v1/main/cis_csi_rx.c（两工程无共享组件机制，改动须双写；与源差异仅此文件头）
 *
 * 移植来源：ESP32_CIS_V1.0/main/cis_detect.c（per-MAC 累加器 44-61、单包平均幅值
 * 130-151、逐包回调 RSSI 突变过滤 153-208、排窗统计 222-248、centi 换算 270-272）。
 * 与 RX 的差异：CSI 打开不走 esp-radar（双初始化冲突，见头注释），改为照抄
 * esp-radar esp32/c3 分支的 4 连调；检测窗由 CIS_CSI_WINDOW_MS esp_timer 周期
 * 排出（替代 radar_cb_ex）；源过滤为软件白名单（其余 3 节点）。
 * TX 不跑 FSM/上报——窗口特征经 cis_now 聚包单播 S3，检测在 S3 侧完成。
 */
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_mac.h"

#include "cis_csi_rx.h"

static const char *TAG = "cis_csi_rx";

/* ---------- per-MAC 逐包累加器（WiFi 回调上下文写，esp_timer 任务读） ---------- */

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

#define CSI_ACC_SLOTS  CIS_CSI_SRC_MAX   /* 槽 4：3 白名单源 + 1 余量 */

static csi_accum_t s_acc[CSI_ACC_SLOTS];
static portMUX_TYPE s_acc_mux = portMUX_INITIALIZER_UNLOCKED;

/* ---------- 白名单与窗口快照 ---------- */

static uint8_t s_src[CIS_CSI_SRC_MAX][6];
static int     s_src_cnt;
static cis_csi_feat_t s_feat[CIS_CSI_SRC_MAX];   /* 各源最新窗口快照（非消费式） */
static uint32_t s_dirty_windows;
static cis_csi_window_fn s_window_hook;
static esp_timer_handle_t s_window_timer;
static bool s_started;

static inline uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000ULL);
}

static int src_find_nolock(const uint8_t mac[6])
{
    for (int i = 0; i < s_src_cnt; i++) {
        if (memcmp(s_src[i], mac, 6) == 0) {
            return i;
        }
    }
    return -1;
}

/* ---------- 单包平均幅值（毫单位，M1 同源算法；TX 用原始全 tone int8 I/Q） ---------- */

static uint32_t csi_mean_amp_milli(const int8_t *d, size_t len)
{
    size_t pairs = len / 2;
    if (!pairs) {
        return 0;
    }
    uint32_t acc = 0;
    for (size_t i = 0; i < pairs; i++) {
        acc += (uint32_t)sqrtf((float)d[2 * i] * d[2 * i] + (float)d[2 * i + 1] * d[2 * i + 1]);
    }
    return (uint32_t)(1000ULL * acc / pairs);
}

/* ---------- esp-radar 同款 4 连调打开 CSI（esp32/c3 分支，esp_radar.c:1608-1626） ---------- */

static void csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    (void)ctx;
    if (!info || !info->buf || info->len < 2) {
        return;
    }
    if (src_find_nolock(info->mac) < 0) {
        return;   /* 软件白名单：源 MAC ∈ {其余 3 节点} */
    }
    uint32_t mean_milli = csi_mean_amp_milli(info->buf, info->len);
    int rssi = info->rx_ctrl.rssi;

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
                slot->rssi_prev = (int16_t)rssi;
                break;
            }
        }
    }
    if (slot) {
        /* RSSI 突变过滤（|ΔRSSI|>6dB 的包不进滑窗，esp-csi issue #185） */
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

/* ---------- CIS_CSI_WINDOW_MS 排窗（esp_timer 任务上下文，替代 radar_cb_ex） ---------- */

static void window_drain_cb(void *arg)
{
    (void)arg;
    if (!s_started) {
        return;
    }
    uint32_t ts = now_ms();

    for (int i = 0; i < CSI_ACC_SLOTS; i++) {
        cis_csi_feat_t f;
        bool have = false;

        portENTER_CRITICAL(&s_acc_mux);
        csi_accum_t *s = &s_acc[i];
        if (s->dirty) {
            s_dirty_windows++;
        }
        if (s->used && s->cnt > 0 && !s->dirty) {
            uint32_t mean_milli = s->acc_milli / s->cnt;
            uint64_t e2 = s->sq_acc_milli / s->cnt;
            uint32_t var_milli2 = (e2 > (uint64_t)mean_milli * mean_milli)
                                  ? (uint32_t)(e2 - (uint64_t)mean_milli * mean_milli) : 0;
            memset(&f, 0, sizeof(f));
            memcpy(f.mac, s->mac, 6);
            f.mean_c = mean_milli / 10;          /* centi-amp（同 cis_detect.c:270） */
            f.var_c  = var_milli2 / 10000;       /* centi-var = raw² × 100 */
            f.p2p_c  = (s->max_milli - s->min_milli) / 10;
            f.rssi   = (int8_t)s->rssi_last;
            f.ts_ms  = ts;
            s_feat[src_find_nolock(s->mac)] = f;
            have = true;
        }
        /* 排空，下一窗重新累计 */
        s->acc_milli = 0;
        s->sq_acc_milli = 0;
        s->cnt = 0;
        s->min_milli = 0;
        s->max_milli = 0;
        s->dirty = false;
        portEXIT_CRITICAL(&s_acc_mux);

        if (have && s_window_hook) {
            s_window_hook(&f);   /* 训练收集（cis_tx_train），须轻量 */
        }
    }
}

/* ---------- 管理接口 ---------- */

esp_err_t cis_csi_rx_init(void)
{
    const uint8_t own[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, (uint8_t)CONFIG_CIS_TX_MAC_LAST};
    static const uint8_t mesh_last[3] = {0x00, 0x01, 0x02};   /* node1 / C3 TX2 / node3 */

    memset(s_acc, 0, sizeof(s_acc));
    memset(s_feat, 0, sizeof(s_feat));
    s_src_cnt = 0;
    s_dirty_windows = 0;
    s_window_hook = NULL;
    s_window_timer = NULL;
    s_started = false;

    /* 白名单 = 其余 3 节点（本节点 MAC 由 Kconfig CIS_TX_NODE 派生，同 app_main.c） */
    for (int i = 0; i < 3 && s_src_cnt < CIS_CSI_SRC_MAX; i++) {
        if (mesh_last[i] == own[5]) {
            continue;
        }
        memcpy(s_src[s_src_cnt], own, 6);
        s_src[s_src_cnt][5] = mesh_last[i];
        s_src_cnt++;
    }
    /* S3 源（起 100Hz 广播后成链路）；MAC 来自 Kconfig CIS_MESH_S3_MAC */
    unsigned v[6];
    if (sscanf(CONFIG_CIS_MESH_S3_MAC, "%x:%x:%x:%x:%x:%x",
               &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) == 6) {
        uint8_t s3[6];
        for (int i = 0; i < 6; i++) {
            s3[i] = (uint8_t)v[i];
        }
        if (src_find_nolock(s3) < 0 && s_src_cnt < CIS_CSI_SRC_MAX) {
            memcpy(s_src[s_src_cnt++], s3, 6);
        }
    } else {
        ESP_LOGW(TAG, "bad CONFIG_CIS_MESH_S3_MAC '%s' — S3 link disabled", CONFIG_CIS_MESH_S3_MAC);
    }

    ESP_LOGI(TAG, "whitelist %d source(s):", s_src_cnt);
    for (int i = 0; i < s_src_cnt; i++) {
        ESP_LOGI(TAG, "  [%d] " MACSTR, i, MAC2STR(s_src[i]));
    }
    return ESP_OK;
}

esp_err_t cis_csi_rx_start(void)
{
    if (s_started) {
        return ESP_OK;
    }
    /* 4 连调照抄 esp-radar esp32/c3 分支（esp_radar.c:1608-1626），
     * csi_config 字段取 esp-radar ESP_RADAR_CSI_CONFIG_DEFAULT 同值 */
    wifi_csi_config_t csi_config = {
        .lltf_en = true,
        .htltf_en = true,
        .stbc_htltf2_en = true,
        .ltf_merge_en = false,
        .channel_filter_en = false,
        .manu_scale = false,
        .shift = 0,
        .dump_ack_en = false,
    };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    const esp_timer_create_args_t timer_args = {
        .callback = &window_drain_cb,
        .name = "cis_csi_win",
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &s_window_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(s_window_timer,
                    (uint64_t)CONFIG_CIS_CSI_WINDOW_MS * 1000ULL));
    s_started = true;
    ESP_LOGI(TAG, "started: window=%dms, csi on (promiscuous+lltf/htltf)", CONFIG_CIS_CSI_WINDOW_MS);
    return ESP_OK;
}

int cis_csi_rx_sources(uint8_t out[][6], int max)
{
    int n = (s_src_cnt < max) ? s_src_cnt : max;
    for (int i = 0; i < n; i++) {
        memcpy(out[i], s_src[i], 6);
    }
    return n;
}

bool cis_csi_rx_source_allowed(const uint8_t mac[6])
{
    return mac && src_find_nolock(mac) >= 0;
}

esp_err_t cis_csi_rx_get_feature(const uint8_t mac[6], cis_csi_feat_t *out)
{
    if (!mac || !out) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_acc_mux);
    int idx = src_find_nolock(mac);
    bool have = (idx >= 0 && s_feat[idx].ts_ms != 0);
    if (have) {
        *out = s_feat[idx];
    }
    portEXIT_CRITICAL(&s_acc_mux);
    return have ? ESP_OK : ESP_ERR_NOT_FOUND;
}

void cis_csi_rx_set_window_hook(cis_csi_window_fn fn)
{
    s_window_hook = fn;
}

uint32_t cis_csi_rx_dropped_windows(void)
{
    return s_dirty_windows;
}
