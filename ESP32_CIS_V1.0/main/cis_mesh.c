/*
 * ESP32_CIS_V1.0 · cis_mesh.c — ESP-NOW 多节点 mesh（S3 RX 汇聚）
 *
 * 职责（多节点升级 2026-10）：
 *   1. 身份：本节点源 MAC 设 1a:00:00:00:00:CONFIG_CIS_NODE_SELF_ID（esp_wifi_set_mac
 *      须在 esp_radar_init 拉起 WiFi 后、STA connect 前——顺序约束同 app_main.c
 *      头注释"WiFi 必须先由 esp-radar 拉起"）。
 *   2. ESP-NOW：esp_now_init + PMK（与 TX 板一致）+ peer 表（ff:ff 广播 + 除自身外
 *      三个 observer 单播 peer，channel=0 随 STA，不加密与 TX 现状一致）。
 *   3. 发送：100Hz 广播 4B 计数器（与 TX 源帧同构，全网 CSI 时钟）。
 *   4. 接收：esp_now_recv_cb 仅 memcpy+入队（WiFi 上下文禁 USB 写/重活）；mesh 任务
 *      解析 FEAT_BUNDLE → cis_detect_feed_external（内部含 cis_report_feature_ex
 *      上报，避免同窗重复入队）走与本地雷达相同的 apply_window FSM 路径；解析
 *      CALIB_RSP → 链路阈值回填（RAM + cis_link NVS）+ 0x14 帧下发。
 *   5. 统计：seq 断流 per 发送方 node（每包 seq 跳变 >1 记丢包，映射到该 observer
 *      的全部链路）；12 槽链路表（最新特征/新鲜度/seq_lost）供 `link list`。
 *
 * 阈值来源：0x14（CALIB_RSP）回填 + 默认 CONFIG_CIS_DEFAULT_THRESHOLD，可选持久化
 * 于 NVS cis_link 命名空间（cis_peer.c cis_link_thr_load/save）。
 *
 * 风险备注（设计文档）：esp_now_init 与 esp-radar（promiscuous）共存未上板验证，
 * 若 recv 回调不触发，可尝试把 esp_now_init 提到 esp_radar_init 之前重试。
 */
#include <string.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_now.h"
#include "esp_mac.h"

#include "cis_mesh.h"
#include "cis_peer.h"
#include "cis_detect.h"
#include "cis_report.h"

static const char *TAG = "cis_mesh";

/* ---------- 状态 ---------- */

typedef struct {
    uint8_t  src[6];
    uint16_t len;
    uint8_t  data[ESP_NOW_MAX_DATA_LEN];
} mesh_rx_msg_t;

#define MESH_RX_QUEUE_LEN 32

typedef struct {
    bool     seen;
    uint16_t last_seq;
    uint32_t pkts;
    uint32_t lost;
    uint32_t last_seen_ms;    /* S3 本地时钟 */
} mesh_node_t;

typedef struct {
    uint8_t  observer;
    uint8_t  mac[6];
    bool     used;
    bool     active;          /* 收到过特征 */
    uint16_t threshold;       /* centi-var（0x14 回填 / NVS / 默认） */
    uint16_t mean_c, var_c, p2p_c;
    int8_t   rssi;
    uint32_t ts_ms;           /* 最新窗口时刻（节点本地 uptime，跨 observer 不可比） */
    uint32_t last_rx_ms;      /* 最近收到该链路数据的 S3 本地时刻（新鲜度用） */
    uint32_t seq_lost;
} mesh_link_t;

static QueueHandle_t s_rx_queue;
static bool s_started = false;

static mesh_node_t s_node[4];
static mesh_link_t s_link[CIS_LINK_MAX];
static uint16_t s_thr_cache[CIS_LINK_MAX];   /* cis_link NVS 镜像（回填后整体保存） */

/* 计数（WiFi 上下文里只做 u32 自增） */
static uint32_t s_rx_ok, s_rx_bad, s_rx_drop, s_rx_src4;
static uint32_t s_tx_bcast, s_tx_cmd, s_tx_fail;
static uint16_t s_tx_seq;                    /* S3 发出的 mesh 帧 seq（CMD_TRAIN） */

static const uint8_t s_bcast_addr[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

static inline uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000ULL);
}

/* ---------- seq 断流统计 ---------- */

static void node_stats_update(uint8_t node, uint16_t seq)
{
    uint32_t ts = now_ms();
    mesh_node_t *n = &s_node[node];
    uint16_t jumped = 0;
    if (n->seen) {
        uint16_t delta = (uint16_t)(seq - n->last_seq);
        if (delta > 1) {
            jumped = (uint16_t)(delta - 1);
            n->lost += jumped;
        }
    }
    n->last_seq = seq;
    n->pkts++;
    n->last_seen_ms = ts;
    n->seen = true;

    if (jumped) {
        for (int i = 0; i < CIS_LINK_MAX; i++) {
            if (s_link[i].used && s_link[i].observer == node) {
                s_link[i].seq_lost += jumped;
            }
        }
    }
}

/* ---------- FEAT_BUNDLE / CALIB_RSP 解析（mesh 任务上下文） ---------- */

static void mesh_handle_bundle(uint8_t observer, uint8_t nrec, const uint8_t *p, uint16_t len)
{
    if (nrec == 0 || nrec > CIS_MESH_MAX_REC || len != (uint16_t)nrec * CIS_MESH_REC_LEN) {
        s_rx_bad++;
        return;
    }
    for (uint8_t i = 0; i < nrec; i++, p += CIS_MESH_REC_LEN) {
        uint8_t src[6];
        memcpy(src, p, 6);
        uint32_t ts   = (uint32_t)p[6] | ((uint32_t)p[7] << 8) |
                        ((uint32_t)p[8] << 16) | ((uint32_t)p[9] << 24);
        uint16_t mean = (uint16_t)(p[10] | (p[11] << 8));
        uint16_t var  = (uint16_t)(p[12] | (p[13] << 8));
        uint16_t p2p  = (uint16_t)(p[14] | (p[15] << 8));
        int8_t rssi   = (int8_t)p[16];

        int slot = cis_link_slot(observer, src[5]);
        if (slot < 0 || memcmp(s_link[slot].mac, src, 6) != 0) {
            s_rx_bad++;
            continue;
        }
        mesh_link_t *l = &s_link[slot];
        l->active = true;
        l->mean_c = mean;
        l->var_c = var;
        l->p2p_c = p2p;
        l->rssi = rssi;
        l->ts_ms = ts;
        l->last_rx_ms = now_ms();

        /* FSM + 特征上报在 apply_window 内完成（与本地雷达同一路径，单次入队） */
        esp_err_t err = cis_detect_feed_external(observer, src, mean, var, p2p, rssi, ts);
        if (err == ESP_ERR_NOT_FOUND) {
            /* 检测上下文丢失（被删/未建）→ 按链路表阈值重建后重喂 */
            cis_detect_peer_ensure(observer, src, l->threshold);
            cis_detect_feed_external(observer, src, mean, var, p2p, rssi, ts);
        }
    }
}

static void mesh_handle_calib(uint8_t observer, const uint8_t *p, uint16_t len)
{
    if (len != 12) {   /* mac(6) + train_s(2) + threshold(2) + noise(2) */
        s_rx_bad++;
        return;
    }
    uint8_t mac[6];
    memcpy(mac, p, 6);
    uint16_t train_s = (uint16_t)(p[6] | (p[7] << 8));
    uint16_t thr     = (uint16_t)(p[8] | (p[9] << 8));
    uint16_t noise   = (uint16_t)(p[10] | (p[11] << 8));

    int slot = cis_link_slot(observer, mac[5]);
    if (slot < 0 || memcmp(s_link[slot].mac, mac, 6) != 0) {
        s_rx_bad++;
        return;
    }
    s_link[slot].active = true;
    s_link[slot].last_rx_ms = now_ms();

    /* 0x14 回填：链路表阈值（RAM）+ cis_link NVS + 检测 ctx */
    if (thr) {
        s_link[slot].threshold = thr;
        s_thr_cache[slot] = thr;
        esp_err_t err = cis_link_thr_save(s_thr_cache);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "cis_link save: %s", esp_err_to_name(err));
        }
        cis_detect_set_threshold(observer, mac, thr);
    }

    /* 0x14 标定·多节点帧 */
    cis_report_train_result_ex(observer, mac, train_s, thr, noise);
    ESP_LOGI(TAG, "calib_rsp %u/" MACSTR ": train=%us thr=%u noise=%u c",
             observer, MAC2STR(mac), train_s, thr, noise);
}

/* ---------- ESP-NOW 回调（WiFi 上下文，保持轻量） ---------- */

static void mesh_recv_cb(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    if (!data || len <= 0) {
        return;
    }
    if (len < CIS_MESH_HDR_LEN) {
        s_rx_src4++;    /* 4B 源帧（TX 100Hz 计数器）等非 mesh 帧，只计数 */
        return;
    }
    mesh_rx_msg_t m = { .len = (len > ESP_NOW_MAX_DATA_LEN) ? ESP_NOW_MAX_DATA_LEN : (uint16_t)len };
    memcpy(m.src, info->src_addr, 6);
    memcpy(m.data, data, m.len);
    if (xQueueSend(s_rx_queue, &m, 0) != pdTRUE) {
        s_rx_drop++;
    }
}

static void mesh_send_cb(const esp_now_send_info_t *tx_info, esp_now_send_status_t status)
{
    (void)tx_info;
    if (status != ESP_NOW_SEND_SUCCESS) {
        s_tx_fail++;
    }
}

/* ---------- 任务 ---------- */

static void mesh_task(void *arg)
{
    (void)arg;
    mesh_rx_msg_t m;
    ESP_LOGI(TAG, "mesh task up: self=%u, feature_rate=%uHz", CIS_OBSERVER_SELF,
             CONFIG_CIS_MESH_FEATURE_RATE_HZ);
    for (;;) {
        if (xQueueReceive(s_rx_queue, &m, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        if (m.len < CIS_MESH_HDR_LEN) {
            s_rx_bad++;
            continue;
        }
        const uint8_t *p = m.data;
        if (p[0] != CIS_MESH_MAGIC0 || p[1] != CIS_MESH_MAGIC1 || p[2] != CIS_MESH_VER) {
            s_rx_bad++;
            continue;
        }
        uint8_t type = p[3];
        uint16_t seq = (uint16_t)(p[4] | (p[5] << 8));
        uint8_t node = p[6];
        uint8_t nrec = p[7];
        if (node > 3 || node == CIS_OBSERVER_SELF) {   /* 自身/非法 node_id 不可能经空口到达 */
            s_rx_bad++;
            continue;
        }
        node_stats_update(node, seq);
        s_rx_ok++;

        switch (type) {
        case CIS_MESH_TYPE_FEAT_BUNDLE:
            mesh_handle_bundle(node, nrec, p + CIS_MESH_HDR_LEN, m.len - CIS_MESH_HDR_LEN);
            break;
        case CIS_MESH_TYPE_CALIB_RSP:
            mesh_handle_calib(node, p + CIS_MESH_HDR_LEN, m.len - CIS_MESH_HDR_LEN);
            break;
        default:
            s_rx_bad++;
            break;
        }
    }
}

/* STA 关联后整表重刷对端信道：mesh_peers_init 必须先于 STA connect 执行（源 MAC
 * 要在 connect 前生效），peer 的 channel=0 在 add 时被固化为当时主信道；STA 关联
 * 路由器后即报 "Peer channel is not equal to the home channel" 全部发送失败。
 * （2026-10-29 真机联调发现：bcast fail=7749 全失败，收方向正常所以特征流仍在。）
 * 广播任务检测连续失败即按当前主信道重刷对端表；SoftAP 降级/换信道场景同样自愈。 */
static void mesh_peers_refresh_channel(void)
{
    uint8_t prim = 0;
    wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
    esp_wifi_get_channel(&prim, &second);
    esp_now_peer_info_t peer = {
        .channel = prim,
        .ifidx   = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(peer.peer_addr, s_bcast_addr, 6);
    esp_now_mod_peer(&peer);
    for (uint8_t obs = 0; obs <= 3; obs++) {
        if (obs == CIS_OBSERVER_SELF) {
            continue;
        }
        memset(peer.peer_addr, 0, 6);   /* 同 mesh_peers_init：先清零再写字节 */
        peer.peer_addr[0] = 0x1a;
        peer.peer_addr[5] = obs;
        esp_now_mod_peer(&peer);      /* 不存在则保持失败态，下轮重试 */
    }
    ESP_LOGW(TAG, "peer channel refreshed to %u (STA %s)", prim,
             prim ? "associated" : "disconnected");
}

/* 100Hz 广播源帧：4B 计数器，与 TX 板 csi_send 同构（全网 CSI 时钟） */
static void mesh_bcast_task(void *arg)
{
    (void)arg;
    uint32_t count = 0;
    int consec_fail = 0;
    const TickType_t period = pdMS_TO_TICKS(1000u / CONFIG_CIS_MESH_SRC_RATE_HZ);
    ESP_LOGI(TAG, "bcast task up: %uHz, 4B counter", CONFIG_CIS_MESH_SRC_RATE_HZ);
    for (;;) {
        if (esp_now_send(s_bcast_addr, (const uint8_t *)&count, sizeof(count)) == ESP_OK) {
            s_tx_bcast++;
            consec_fail = 0;
        } else {
            s_tx_fail++;
            /* 50 次（0.5s）失败后重刷一次；之后每 500 次（5s）重试直到恢复 */
            if (++consec_fail == 50 || (consec_fail > 50 && consec_fail % 500 == 0)) {
                mesh_peers_refresh_channel();
            }
        }
        count++;
        vTaskDelay(period);
    }
}

/* ---------- 启动 ---------- */

static esp_err_t mesh_peers_init(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));   /* 与 TX 板一致 */

    esp_err_t err;
    esp_now_peer_info_t peer = {
        .channel = 0,          /* 0 = 跟随当前 STA 信道（ROUTER 同路由 → 全网同信道） */
        .ifidx   = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(peer.peer_addr, s_bcast_addr, 6);
    err = esp_now_add_peer(&peer);
    if (err != ESP_OK && err != ESP_ERR_ESPNOW_EXIST) {
        return err;
    }
    for (uint8_t obs = 0; obs <= 3; obs++) {
        if (obs == CIS_OBSERVER_SELF) {
            continue;
        }
        esp_now_peer_info_t tx_peer = peer;
        /* peer_addr 此刻是广播地址 FF:FF:FF:FF:FF:FF，必须整块清零再写——
         * 只改 [0]/[5] 会把中间 4 字节留成 FF，加进 1a:FF:FF:FF:FF:0x，
         * 之后所有 CMD_TRAIN 单播 ESP_ERR_ESPNOW_NOT_FOUND（2026-10-30 真机） */
        memset(tx_peer.peer_addr, 0, 6);
        tx_peer.peer_addr[0] = 0x1a;
        tx_peer.peer_addr[5] = obs;
        err = esp_now_add_peer(&tx_peer);
        if (err != ESP_OK && err != ESP_ERR_ESPNOW_EXIST) {
            return err;
        }
    }
    return ESP_OK;
}

static void mesh_links_init(void)
{
    uint16_t thr[CIS_LINK_MAX];
    cis_link_thr_load(thr);
    memcpy(s_thr_cache, thr, sizeof(s_thr_cache));

    uint8_t mac[6];
    int n = 0;
    for (uint8_t obs = 0; obs <= 3; obs++) {
        for (uint8_t src = 0; src <= 3; src++) {
            int slot = cis_link_slot(obs, src);
            if (slot < 0) {
                continue;   /* observer == source 不成链路 */
            }
            memset(&s_link[slot], 0, sizeof(s_link[slot]));
            s_link[slot].observer = obs;
            mac[0] = 0x1a; mac[1] = 0x00; mac[2] = 0x00;
            mac[3] = 0x00; mac[4] = 0x00; mac[5] = src;
            memcpy(s_link[slot].mac, mac, 6);
            s_link[slot].used = true;
            s_link[slot].threshold = thr[slot] ? thr[slot] : (uint16_t)CONFIG_CIS_DEFAULT_THRESHOLD;
            if (obs != CIS_OBSERVER_SELF) {
                /* 远端链路：预建检测上下文（本地链路由 peer add/cis_peer_load 建） */
                cis_detect_peer_ensure(obs, mac, s_link[slot].threshold);
            }
            n++;
        }
    }
    ESP_LOGI(TAG, "link table ready: %d links (thr from cis_link NVS / default %u c)",
             n, CONFIG_CIS_DEFAULT_THRESHOLD);
}

esp_err_t cis_mesh_start(void)
{
    if (s_started) {
        return ESP_OK;
    }
#if CONFIG_CIS_MESH_ENABLE
    /* 1. 身份：esp_radar_init 已拉起 WiFi（esp_radar_wifi_init），STA connect 尚未发生
     *    （cis_wifi_start 在 app_main 里后于此调用），此刻设源 MAC 安全。 */
    uint8_t self_mac[6];
    cis_self_source_mac(self_mac);
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, self_mac));
    ESP_LOGI(TAG, "STA source MAC set to " MACSTR " (observer_id=0x%02x)",
             MAC2STR(self_mac), CIS_OBSERVER_SELF);

    /* 2. ESP-NOW：init + PMK + peer 表 */
    esp_err_t err = mesh_peers_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "espnow peers init: %s", esp_err_to_name(err));
        return err;
    }
    ESP_ERROR_CHECK(esp_now_register_recv_cb(mesh_recv_cb));
    ESP_ERROR_CHECK(esp_now_register_send_cb(mesh_send_cb));

    /* 3. 链路表（阈值来自 cis_link NVS / 默认）+ 远端检测上下文 */
    mesh_links_init();

    /* 4. 收发任务 */
    s_rx_queue = xQueueCreate(MESH_RX_QUEUE_LEN, sizeof(mesh_rx_msg_t));
    if (!s_rx_queue) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(mesh_task, "cis_mesh", 4096, NULL, 4, NULL) != pdPASS ||
            xTaskCreate(mesh_bcast_task, "cis_bcast", 3072, NULL, 3, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    s_started = true;
    return ESP_OK;
#else
    ESP_LOGI(TAG, "mesh disabled (CONFIG_CIS_MESH_ENABLE=n)");
    return ESP_OK;
#endif
}

/* ---------- 对外接口 ---------- */

esp_err_t cis_mesh_train(uint8_t observer, const uint8_t mac[6], uint16_t seconds)
{
    if (!mac) {
        return ESP_ERR_INVALID_ARG;
    }
    if (observer > 3) {
        return ESP_ERR_INVALID_ARG;
    }
    if (observer == CIS_OBSERVER_SELF) {
        return cis_detect_train_start(observer, mac, seconds);
    }
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }

    /* CMD_TRAIN 单播：hdr(8) + mac(6) + seconds(2) */
    uint8_t buf[CIS_MESH_HDR_LEN + 8];
    uint16_t seq = ++s_tx_seq;
    buf[0] = CIS_MESH_MAGIC0;
    buf[1] = CIS_MESH_MAGIC1;
    buf[2] = CIS_MESH_VER;
    buf[3] = CIS_MESH_TYPE_CMD_TRAIN;
    buf[4] = (uint8_t)(seq & 0xFF);
    buf[5] = (uint8_t)(seq >> 8);
    buf[6] = CIS_OBSERVER_SELF;
    buf[7] = 0;                          /* nrec 仅 FEAT_BUNDLE 使用 */
    memcpy(buf + CIS_MESH_HDR_LEN, mac, 6);
    buf[CIS_MESH_HDR_LEN + 6] = (uint8_t)(seconds & 0xFF);
    buf[CIS_MESH_HDR_LEN + 7] = (uint8_t)(seconds >> 8);

    uint8_t dst[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, observer};
    esp_err_t err = esp_now_send(dst, buf, sizeof(buf));
    if (err == ESP_OK) {
        s_tx_cmd++;
        ESP_LOGI(TAG, "CMD_TRAIN -> observer %u (" MACSTR ") mac=" MACSTR " %us",
                 observer, MAC2STR(dst), MAC2STR(mac), seconds);
    } else {
        s_tx_fail++;
        ESP_LOGW(TAG, "CMD_TRAIN -> observer %u failed: %s", observer, esp_err_to_name(err));
    }
    return err;
}

bool cis_mesh_link_get(size_t i, cis_mesh_link_info_t *out)
{
    if (!out || i >= CIS_LINK_MAX || !s_started) {
        return false;
    }
    mesh_link_t *l = &s_link[i];
    memset(out, 0, sizeof(*out));
    out->observer = l->observer;
    memcpy(out->mac, l->mac, 6);
    out->threshold = l->threshold;
    out->seq_lost = l->seq_lost;

    if (l->observer == CIS_OBSERVER_SELF) {
        /* 本地链路：实时状态以 cis_detect 为准（peer 注册后才有 ctx） */
        cis_det_stats_t st;
        if (cis_detect_get_stats(CIS_OBSERVER_SELF, l->mac, &st) == ESP_OK) {
            out->registered = true;
            out->active = st.ts_ms != 0;
            out->mean_c = st.mean_c;
            out->var_c = st.var_c;
            out->p2p_c = st.p2p_c;
            out->rssi = st.rssi;
            out->ts_ms = st.ts_ms;
            out->age_ms = st.ts_ms ? now_ms() - st.ts_ms : 0;
            out->threshold = st.threshold;
        }
    } else {
        out->active = l->active;
        out->mean_c = l->mean_c;
        out->var_c = l->var_c;
        out->p2p_c = l->p2p_c;
        out->rssi = l->rssi;
        out->ts_ms = l->ts_ms;
        out->age_ms = l->active ? now_ms() - l->last_rx_ms : 0;
    }
    return true;
}

bool cis_mesh_node_get(size_t i, uint8_t *observer, bool *seen,
                       uint32_t *age_ms, uint32_t *pkts, uint32_t *lost)
{
    if (i >= 4 || !s_started) {
        return false;
    }
    mesh_node_t *n = &s_node[i];
    if (observer) {
        *observer = (uint8_t)i;
    }
    if (seen) {
        *seen = n->seen;
    }
    if (age_ms) {
        *age_ms = n->seen ? now_ms() - n->last_seen_ms : 0;
    }
    if (pkts) {
        *pkts = n->pkts;
    }
    if (lost) {
        *lost = n->lost;
    }
    return true;
}

void cis_mesh_status(char *out, size_t len)
{
    if (!s_started) {
        snprintf(out, len, "mesh: off");
        return;
    }
    uint32_t active = 0;
    for (int i = 0; i < CIS_LINK_MAX; i++) {
        if (s_link[i].active) {
            active++;
        }
    }
    snprintf(out, len,
             "espnow rx ok=%lu bad=%lu drop=%lu src4B=%lu | tx bcast=%lu cmd=%lu fail=%lu "
             "| links %lu/%u",
             (unsigned long)s_rx_ok, (unsigned long)s_rx_bad, (unsigned long)s_rx_drop,
             (unsigned long)s_rx_src4, (unsigned long)s_tx_bcast, (unsigned long)s_tx_cmd,
             (unsigned long)s_tx_fail, (unsigned long)active, CIS_LINK_MAX);
}
