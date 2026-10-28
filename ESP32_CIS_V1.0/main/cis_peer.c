/*
 * ESP32_CIS_V1.0 · cis_peer.c — peer 表 + NVS 持久化 + 最近 CSI 源 MAC 扫描表
 */
#include <string.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_timer.h"
#include "esp_mac.h"
#include "esp_radar.h"

#include "cis_peer.h"
#include "cis_detect.h"

static const char *TAG = "cis_peer";
#define NVS_NS      "cis_peer"
#define NVS_KEY     "table"
#define NVS_MAGIC   0x43495331UL    /* "CIS1" */
#define SCAN_SLOTS  8

static cis_peer_entry_t s_table[CIS_PEER_MAX];
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

typedef struct {
    uint8_t  mac[6];
    int8_t   rssi;
    uint32_t last_seen_ms;
    uint32_t pkt_cnt;
} scan_slot_t;

static scan_slot_t s_scan[SCAN_SLOTS];

static inline uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000ULL);
}

/* ---- NVS 序列化：magic(4) + used 位图(1) + 每槽 [mac6|name16|thr2] ---- */
#define REC_SIZE (6 + CIS_PEER_NAME_LEN + 2)
#define BLOB_SIZE (5 + CIS_PEER_MAX * REC_SIZE)

static void table_serialize(uint8_t *blob)
{
    memset(blob, 0, BLOB_SIZE);
    blob[0] = (uint8_t)(NVS_MAGIC & 0xFF);
    blob[1] = (uint8_t)((NVS_MAGIC >> 8) & 0xFF);
    blob[2] = (uint8_t)((NVS_MAGIC >> 16) & 0xFF);
    blob[3] = (uint8_t)((NVS_MAGIC >> 24) & 0xFF);
    uint8_t used_bits = 0;
    for (int i = 0; i < CIS_PEER_MAX; i++) {
        uint8_t *rec = blob + 5 + i * REC_SIZE;
        if (s_table[i].used) {
            used_bits |= (uint8_t)(1U << i);
            memcpy(rec, s_table[i].mac, 6);
            memcpy(rec + 6, s_table[i].name, CIS_PEER_NAME_LEN);
            rec[6 + CIS_PEER_NAME_LEN] = (uint8_t)(s_table[i].threshold & 0xFF);
            rec[6 + CIS_PEER_NAME_LEN + 1] = (uint8_t)(s_table[i].threshold >> 8);
        }
    }
    blob[4] = used_bits;
}

static void table_deserialize(const uint8_t *blob)
{
    uint32_t magic = (uint32_t)blob[0] | ((uint32_t)blob[1] << 8) |
                     ((uint32_t)blob[2] << 16) | ((uint32_t)blob[3] << 24);
    if (magic != NVS_MAGIC) {
        ESP_LOGW(TAG, "peer table magic mismatch: 0x%08lx, start empty", (unsigned long)magic);
        return;
    }
    uint8_t used_bits = blob[4];
    for (int i = 0; i < CIS_PEER_MAX; i++) {
        if (!(used_bits & (1U << i))) {
            continue;
        }
        const uint8_t *rec = blob + 5 + i * REC_SIZE;
        s_table[i].used = true;
        memcpy(s_table[i].mac, rec, 6);
        memcpy(s_table[i].name, rec + 6, CIS_PEER_NAME_LEN - 1);
        s_table[i].name[CIS_PEER_NAME_LEN - 1] = '\0';
        s_table[i].threshold = (uint16_t)(rec[6 + CIS_PEER_NAME_LEN] |
                                          ((uint16_t)rec[6 + CIS_PEER_NAME_LEN + 1] << 8));
    }
}

esp_err_t cis_peer_save(void)
{
    uint8_t blob[BLOB_SIZE];
    esp_err_t err;
    portENTER_CRITICAL(&s_lock);
    table_serialize(blob);
    portEXIT_CRITICAL(&s_lock);

    nvs_handle_t h;
    err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_blob(h, NVS_KEY, blob, BLOB_SIZE);
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    return err;
}

void cis_peer_load(void)
{
    size_t len = BLOB_SIZE;
    uint8_t blob[BLOB_SIZE];
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        esp_err_t err = nvs_get_blob(h, NVS_KEY, blob, &len);
        nvs_close(h);
        if (err == ESP_OK && len == BLOB_SIZE) {
            table_deserialize(blob);
        } else {
            ESP_LOGW(TAG, "no valid peer table in NVS (err=%s len=%d), start empty",
                     esp_err_to_name(err), (int)len);
        }
    }

    int n = 0;
    for (int i = 0; i < CIS_PEER_MAX; i++) {
        if (!s_table[i].used) {
            continue;
        }
        esp_radar_handle_t handle = NULL;
        esp_err_t err = esp_radar_new_peer(s_table[i].mac, &handle);
        if (err != ESP_OK && err != ESP_ERR_INVALID_RESPONSE) {
            ESP_LOGE(TAG, "peer " MACSTR " esp_radar_new_peer: %s",
                     MAC2STR(s_table[i].mac), esp_err_to_name(err));
            continue;
        }
        cis_detect_peer_ensure(CIS_OBSERVER_SELF, s_table[i].mac, s_table[i].threshold);
        ESP_LOGI(TAG, "restored peer[%d] " MACSTR " name=%s thr=%u",
                 i, MAC2STR(s_table[i].mac), s_table[i].name, s_table[i].threshold);
        n++;
    }
    ESP_LOGI(TAG, "peer table loaded: %d peer(s)", n);
}

esp_err_t cis_peer_add(const uint8_t mac[6], const char *name)
{
    if (!mac || cis_peer_find(mac) >= 0) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_radar_handle_t handle = NULL;
    esp_err_t err = esp_radar_new_peer(mac, &handle);
    if (err != ESP_OK && err != ESP_ERR_INVALID_RESPONSE) {
        ESP_LOGE(TAG, "esp_radar_new_peer: %s", esp_err_to_name(err));
        return err;
    }

    int slot = -1;
    portENTER_CRITICAL(&s_lock);
    for (int i = 0; i < CIS_PEER_MAX; i++) {
        if (!s_table[i].used) {
            slot = i;
            break;
        }
    }
    if (slot >= 0) {
        cis_peer_entry_t *e = &s_table[slot];
        memset(e, 0, sizeof(*e));
        memcpy(e->mac, mac, 6);
        if (name && name[0]) {
            strlcpy(e->name, name, CIS_PEER_NAME_LEN);
        } else {
            snprintf(e->name, CIS_PEER_NAME_LEN, "zone%d", slot);
        }
        e->used = true;
    }
    portEXIT_CRITICAL(&s_lock);

    if (slot < 0) {
        return ESP_ERR_NO_MEM;
    }
    cis_detect_peer_ensure(CIS_OBSERVER_SELF, mac, s_table[slot].threshold);
    err = cis_peer_save();
    ESP_LOGI(TAG, "peer[%d] added: " MACSTR " name=%s (%s)", slot, MAC2STR(mac),
             s_table[slot].name, esp_err_to_name(err));
    return err;
}

esp_err_t cis_peer_del(const uint8_t mac[6])
{
    int slot = cis_peer_find(mac);
    if (slot < 0) {
        return ESP_ERR_NOT_FOUND;
    }

    /* esp_radar_del_peer 要求雷达处于 stop 状态；短暂中断检测 */
    esp_err_t err = esp_radar_stop();
    if (err == ESP_OK) {
        /* 默认 peer（wildcard）不可删；只有我们自己 new 的才需要 del。
           找不到 handle 时忽略——esp-radar 侧由 unicast 表自动去重。 */
        ESP_LOGI(TAG, "radar stopped for peer del");
    } else {
        ESP_LOGW(TAG, "esp_radar_stop: %s", esp_err_to_name(err));
    }

    portENTER_CRITICAL(&s_lock);
    memset(&s_table[slot], 0, sizeof(s_table[slot]));
    portEXIT_CRITICAL(&s_lock);
    cis_detect_peer_remove(CIS_OBSERVER_SELF, mac);

    err = cis_peer_save();
    if (esp_radar_start() != ESP_OK) {
        ESP_LOGE(TAG, "esp_radar_start failed after peer del!");
    }
    ESP_LOGI(TAG, "peer[%d] deleted " MACSTR " (%s)", slot, MAC2STR(mac), esp_err_to_name(err));
    return err;
}

int cis_peer_find(const uint8_t mac[6])
{
    int found = -1;
    portENTER_CRITICAL(&s_lock);
    for (int i = 0; i < CIS_PEER_MAX; i++) {
        if (s_table[i].used && memcmp(s_table[i].mac, mac, 6) == 0) {
            found = i;
            break;
        }
    }
    portEXIT_CRITICAL(&s_lock);
    return found;
}

const cis_peer_entry_t *cis_peer_get(int slot)
{
    if (slot < 0 || slot >= CIS_PEER_MAX) {
        return NULL;
    }
    return s_table[slot].used ? &s_table[slot] : NULL;
}

uint8_t cis_peer_count(void)
{
    uint8_t n = 0;
    portENTER_CRITICAL(&s_lock);
    for (int i = 0; i < CIS_PEER_MAX; i++) {
        if (s_table[i].used) {
            n++;
        }
    }
    portEXIT_CRITICAL(&s_lock);
    return n;
}

esp_err_t cis_peer_set_threshold(const uint8_t mac[6], uint16_t threshold_c)
{
    int slot = cis_peer_find(mac);
    if (slot < 0) {
        return ESP_ERR_NOT_FOUND;
    }
    portENTER_CRITICAL(&s_lock);
    s_table[slot].threshold = threshold_c;
    portEXIT_CRITICAL(&s_lock);
    cis_detect_set_threshold(CIS_OBSERVER_SELF, mac, threshold_c);
    return ESP_OK;
}

/* ---- 最近源 MAC 扫描表 ---- */

void cis_peer_note_csi(const uint8_t mac[6], int rssi)
{
    uint32_t ts = now_ms();
    portENTER_CRITICAL(&s_lock);
    scan_slot_t *free_slot = NULL, *hit = NULL, *oldest = &s_scan[0];
    for (int i = 0; i < SCAN_SLOTS; i++) {
        scan_slot_t *s = &s_scan[i];
        if (s->last_seen_ms == 0) {
            if (!free_slot) {
                free_slot = s;
            }
            continue;
        }
        if (memcmp(s->mac, mac, 6) == 0) {
            hit = s;
            break;
        }
        if (s->last_seen_ms < oldest->last_seen_ms) {
            oldest = s;
        }
    }
    scan_slot_t *t = hit ? hit : (free_slot ? free_slot : oldest);
    if (!hit) {
        memcpy(t->mac, mac, 6);
        t->pkt_cnt = 0;
    }
    t->rssi = (int8_t)rssi;
    t->last_seen_ms = ts;
    t->pkt_cnt++;
    portEXIT_CRITICAL(&s_lock);
}

bool cis_peer_scan_get(size_t i, uint8_t mac_out[6], int *last_rssi, uint32_t *age_ms)
{
    scan_slot_t *sorted[SCAN_SLOTS] = {0};
    size_t n = 0;
    uint32_t ts = now_ms();

    portENTER_CRITICAL(&s_lock);
    for (int k = 0; k < SCAN_SLOTS; k++) {
        if (s_scan[k].last_seen_ms) {
            sorted[n++] = &s_scan[k];
        }
    }
    /* 插入排序：最近活跃在前（槽位少，直接在临界区内排） */
    for (size_t a = 1; a < n; a++) {
        scan_slot_t *key = sorted[a];
        size_t b = a;
        while (b > 0 && sorted[b - 1]->last_seen_ms < key->last_seen_ms) {
            sorted[b] = sorted[b - 1];
            b--;
        }
        sorted[b] = key;
    }
    if (i < n) {
        if (mac_out) {
            memcpy(mac_out, sorted[i]->mac, 6);
        }
        if (last_rssi) {
            *last_rssi = sorted[i]->rssi;
        }
        if (age_ms) {
            *age_ms = ts - sorted[i]->last_seen_ms;
        }
    }
    portEXIT_CRITICAL(&s_lock);
    return i < n;
}

/* ---- 链路阈值持久化（cis_link NVS 命名空间：magic + 12×u16，链路槽位序） ---- */

#define LINK_NVS_NS   "cis_link"
#define LINK_NVS_KEY  "thr"
#define LINK_MAGIC    0x4C4E4B31UL    /* "LNK1" */
#define LINK_BLOB_SIZE (4 + CIS_LINK_MAX * 2)

void cis_link_thr_load(uint16_t thr[CIS_LINK_MAX])
{
    memset(thr, 0, CIS_LINK_MAX * sizeof(uint16_t));
    uint8_t blob[LINK_BLOB_SIZE];
    size_t len = sizeof(blob);
    nvs_handle_t h;
    if (nvs_open(LINK_NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        return;   /* 首次启动无 blob → 全 0 = 各链路用默认阈值 */
    }
    esp_err_t err = nvs_get_blob(h, LINK_NVS_KEY, blob, &len);
    nvs_close(h);
    if (err != ESP_OK || len != LINK_BLOB_SIZE) {
        ESP_LOGW(TAG, "no valid cis_link blob (err=%s len=%d), thresholds default",
                 esp_err_to_name(err), (int)len);
        return;
    }
    uint32_t magic = (uint32_t)blob[0] | ((uint32_t)blob[1] << 8) |
                     ((uint32_t)blob[2] << 16) | ((uint32_t)blob[3] << 24);
    if (magic != LINK_MAGIC) {
        ESP_LOGW(TAG, "cis_link magic mismatch: 0x%08lx, thresholds default",
                 (unsigned long)magic);
        return;
    }
    for (int i = 0; i < CIS_LINK_MAX; i++) {
        thr[i] = (uint16_t)(blob[4 + i * 2] | ((uint16_t)blob[4 + i * 2 + 1] << 8));
    }
    ESP_LOGI(TAG, "cis_link thresholds loaded");
}

esp_err_t cis_link_thr_save(const uint16_t thr[CIS_LINK_MAX])
{
    uint8_t blob[LINK_BLOB_SIZE] = {0};
    blob[0] = (uint8_t)(LINK_MAGIC & 0xFF);
    blob[1] = (uint8_t)((LINK_MAGIC >> 8) & 0xFF);
    blob[2] = (uint8_t)((LINK_MAGIC >> 16) & 0xFF);
    blob[3] = (uint8_t)((LINK_MAGIC >> 24) & 0xFF);
    for (int i = 0; i < CIS_LINK_MAX; i++) {
        blob[4 + i * 2] = (uint8_t)(thr[i] & 0xFF);
        blob[4 + i * 2 + 1] = (uint8_t)(thr[i] >> 8);
    }
    nvs_handle_t h;
    esp_err_t err = nvs_open(LINK_NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_blob(h, LINK_NVS_KEY, blob, LINK_BLOB_SIZE);
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    return err;
}
