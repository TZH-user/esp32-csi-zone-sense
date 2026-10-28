/*
 * csi_tx_v1 · cis_now.c — mesh ESP-NOW 上行/下行实现（见 cis_now.h 头注释）
 *
 * 线程模型：全部 esp_now_send 都在打包任务上下文（seq 无需加锁）；
 * recv 回调（WiFi 任务）只做 magic/长度过滤 + 入队，绝不在回调里发送。
 * RX 队列仅收 ef 帧（对端 TX 的 100Hz 源帧是 4B 裸计数，magic 不符即丢）。
 */
#include <string.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_now.h"
#include "esp_mac.h"

#include "cis_csi_rx.h"
#include "cis_tx_train.h"
#include "cis_now.h"

static const char *TAG = "cis_now";

/* 与 app_main.c CONFIG_ESP_NOW_PHYMODE/CONFIG_ESP_NOW_RATE 保持一致：
 * AP 关联为 802.11b/g，ESP-NOW 统一 11G/6M（特征包 ~130µs @10Hz ≈ 0.13% 占空） */
#define CIS_ESPNOW_PHYMODE   WIFI_PHY_MODE_11G
#define CIS_ESPNOW_RATE      WIFI_PHY_RATE_6M

typedef struct {
    uint8_t  src[6];
    uint16_t len;
    uint8_t  data[ESP_NOW_MAX_DATA_LEN];
} now_rx_msg_t;

#define RX_QUEUE_LEN  8

static QueueHandle_t s_rx_queue;
static uint8_t s_s3_mac[6];
static uint8_t s_node_id;
static uint16_t s_seq;
static uint8_t s_txbuf[8 + 14 * sizeof(cis_ef_feat_rec_t)];   /* nrec≤14 @250B */

/* 每白名单源上次已发送的窗 ts（去重：同一窗不重发） */
static uint32_t s_last_sent_ts[CIS_CSI_SRC_MAX];

static const char *ef_type_name(uint8_t type)
{
    switch (type) {
    case CIS_EFT_FEAT_BUNDLE: return "FEAT_BUNDLE";
    case CIS_EFT_CALIB_RSP:   return "CALIB_RSP";
    case CIS_EFT_CMD_TRAIN:   return "CMD_TRAIN";
    default:                  return "?";
    }
}

static bool parse_mac6(const char *str, uint8_t out[6])
{
    unsigned v[6];
    if (!str || sscanf(str, "%x:%x:%x:%x:%x:%x",
                       &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6) {
        return false;
    }
    for (int i = 0; i < 6; i++) {
        if (v[i] > 0xFF) {
            return false;
        }
        out[i] = (uint8_t)v[i];
    }
    return true;
}

static inline uint16_t clamp_u16(uint32_t v)
{
    return (v > 0xFFFFu) ? 0xFFFFu : (uint16_t)v;
}

/* ---- 发送（仅打包任务调用） ---- */

static size_t ef_put_hdr(uint8_t *buf, uint8_t type, uint8_t nrec)
{
    cis_ef_hdr_t h = {
        .magic0 = CIS_EF_SYNC0,
        .magic1 = CIS_EF_SYNC1,
        .ver = CIS_EF_VER,
        .type = type,
        .seq = s_seq++,
        .node_id = s_node_id,
        .nrec = nrec,
    };
    memcpy(buf, &h, sizeof(h));
    return sizeof(h);
}

static void ef_send(uint8_t type, uint8_t nrec, size_t len)
{
    ef_put_hdr(s_txbuf, type, nrec);
    esp_err_t err = esp_now_send(s_s3_mac, s_txbuf, len);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "esp_now_send %s failed: %s", ef_type_name(type), esp_err_to_name(err));
    }
}

/* ---- 接收（WiFi 回调 → 队列 → 打包任务处理） ---- */

static void now_recv_cb(const esp_now_recv_info_t *info, const uint8_t *data, int data_len)
{
    if (!info || !data || data_len < (int)sizeof(cis_ef_hdr_t) ||
            data_len > (int)sizeof(((now_rx_msg_t *)0)->data)) {
        return;
    }
    if (data[0] != CIS_EF_SYNC0 || data[1] != CIS_EF_SYNC1 || data[2] != CIS_EF_VER) {
        return;   /* 对端 TX 源帧（4B 计数）等非 ef 流量在此丢弃 */
    }
    now_rx_msg_t m = { .len = (uint16_t)data_len };
    memcpy(m.src, info->src_addr, 6);
    memcpy(m.data, data, data_len);
    if (s_rx_queue) {
        xQueueSend(s_rx_queue, &m, 0);   /* 队列满即丢（CMD_TRAIN 由 S3 重发） */
    }
}

static void handle_cmd_train(const now_rx_msg_t *m)
{
    if (memcmp(m->src, s_s3_mac, 6) != 0) {
        return;   /* CMD_TRAIN 只认 S3 单播 */
    }
    if (m->len != sizeof(cis_ef_hdr_t) + sizeof(cis_ef_cmd_train_t)) {
        ESP_LOGW(TAG, "CMD_TRAIN bad len %u", m->len);
        return;
    }
    const cis_ef_cmd_train_t *cmd = (const cis_ef_cmd_train_t *)
                                    (m->data + sizeof(cis_ef_hdr_t));
    esp_err_t err = cis_tx_train_start(cmd->mac, cmd->seconds);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "CMD_TRAIN " MACSTR " %us (seq=%u)",
                 MAC2STR(cmd->mac), cmd->seconds,
                 ((const cis_ef_hdr_t *)m->data)->seq);
    } else {
        ESP_LOGW(TAG, "CMD_TRAIN " MACSTR " rejected: %s",
                 MAC2STR(cmd->mac), esp_err_to_name(err));
    }
}

static void process_rx_queue(void)
{
    now_rx_msg_t m;
    while (xQueueReceive(s_rx_queue, &m, 0) == pdTRUE) {
        const cis_ef_hdr_t *h = (const cis_ef_hdr_t *)m.data;
        switch (h->type) {
        case CIS_EFT_CMD_TRAIN:
            handle_cmd_train(&m);
            break;
        default:
            ESP_LOGD(TAG, "ignore ef type 0x%02x from " MACSTR, h->type, MAC2STR(m.src));
            break;
        }
    }
}

/* ---- 训练完成 → CALIB_RSP ---- */

static void send_calib_rsp(const cis_tx_train_result_t *r)
{
    cis_ef_calib_t cal = {
        .train_s = r->train_s,
        .threshold = r->threshold_c,
        .noise_floor = r->noise_c,
    };
    memcpy(cal.mac, r->mac, 6);
    memcpy(s_txbuf + sizeof(cis_ef_hdr_t), &cal, sizeof(cal));
    ef_send(CIS_EFT_CALIB_RSP, 0, sizeof(cis_ef_hdr_t) + sizeof(cal));
    ESP_LOGI(TAG, "CALIB_RSP " MACSTR ": train_s=%u thr=%u noise=%u",
             MAC2STR(r->mac), r->train_s, r->threshold_c, r->noise_c);
}

/* ---- FEAT_BUNDLE 打包（10Hz，聚合全部白名单链路最新窗口） ---- */

static void send_feat_bundle(void)
{
    uint8_t srcs[CIS_CSI_SRC_MAX][6];
    int n = cis_csi_rx_sources(srcs, CIS_CSI_SRC_MAX);
    uint8_t nrec = 0;

    for (int i = 0; i < n; i++) {
        cis_csi_feat_t f;
        if (cis_csi_rx_get_feature(srcs[i], &f) != ESP_OK || f.ts_ms == 0 ||
                f.ts_ms == s_last_sent_ts[i]) {
            continue;   /* 无新窗（源静默/空窗/RSSI 突变丢窗） */
        }
        cis_ef_feat_rec_t rec = {
            .ts_ms = f.ts_ms,
            .mean = clamp_u16(f.mean_c),
            .var = clamp_u16(f.var_c),
            .p2p = clamp_u16(f.p2p_c),
            .rssi = f.rssi,
        };
        memcpy(rec.mac, f.mac, 6);
        memcpy(s_txbuf + sizeof(cis_ef_hdr_t) + (size_t)nrec * sizeof(rec), &rec, sizeof(rec));
        nrec++;
        s_last_sent_ts[i] = f.ts_ms;
    }
    if (nrec) {
        ef_send(CIS_EFT_FEAT_BUNDLE, nrec, sizeof(cis_ef_hdr_t) + (size_t)nrec * sizeof(cis_ef_feat_rec_t));
    }
}

static void pack_task(void *arg)
{
    (void)arg;
    const TickType_t interval = pdMS_TO_TICKS(1000u / CONFIG_CIS_MESH_FEATURE_RATE_HZ);
    TickType_t last_wake = xTaskGetTickCount();

    ESP_LOGI(TAG, "pack task: rate=%dHz → S3 " MACSTR " (node_id=0x%02x)",
             CONFIG_CIS_MESH_FEATURE_RATE_HZ, MAC2STR(s_s3_mac), s_node_id);

    for (;;) {
        vTaskDelayUntil(&last_wake, interval);

        cis_tx_train_result_t tr;
        while (cis_tx_train_poll_done(&tr)) {   /* NVS 落盘在 poll_done 内完成 */
            send_calib_rsp(&tr);
        }
        process_rx_queue();
        send_feat_bundle();
    }
}

/* ---- 初始化 ---- */

esp_err_t cis_now_init(void)
{
    if (!parse_mac6(CONFIG_CIS_MESH_S3_MAC, s_s3_mac)) {
        ESP_LOGE(TAG, "bad CONFIG_CIS_MESH_S3_MAC '%s'", CONFIG_CIS_MESH_S3_MAC);
        return ESP_ERR_INVALID_ARG;
    }
    uint8_t own[6];
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, own));
    s_node_id = own[5];   /* node_id ≡ 源 MAC[5]（observer_id 同源） */

    s_rx_queue = xQueueCreate(RX_QUEUE_LEN, sizeof(now_rx_msg_t));
    if (!s_rx_queue) {
        return ESP_ERR_NO_MEM;
    }

    /* S3 单播 peer（channel=0 随 STA，不加密——与 app_main.c 广播 peer 一致） */
    esp_now_peer_info_t peer = {
        .channel = 0,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(peer.peer_addr, s_s3_mac, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate_config = {
        .phymode = CIS_ESPNOW_PHYMODE,
        .rate = CIS_ESPNOW_RATE,
        .ersu = false,
        .dcm = false,
    };
    esp_err_t rate_err = esp_now_set_peer_rate_config(peer.peer_addr, &rate_config);
    if (rate_err != ESP_OK) {
        ESP_LOGW(TAG, "esp_now_set_peer_rate_config failed: %s, using default rate",
                 esp_err_to_name(rate_err));
    }

    ESP_ERROR_CHECK(esp_now_register_recv_cb(&now_recv_cb));

    if (xTaskCreate(pack_task, "cis_now", 4096, NULL, 5, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
