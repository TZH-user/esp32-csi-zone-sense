/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
/* Get Start Example

   This example code is in the Public Domain (or CC0 licensed, at your option.)

   Unless required by applicable law or agreed to in writing, this
   software is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
   CONDITIONS OF ANY KIND, either express or implied.
*/
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <assert.h>

#include "nvs_flash.h"

#include "esp_mac.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_now.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

#include "cis_csi_rx.h"
#include "cis_tx_train.h"
#include "cis_now.h"

#define CONFIG_LESS_INTERFERENCE_CHANNEL   6 /* used by C5/C6 paths only; esp32 STA follows AP channel */
#define CONFIG_WIFI_SSID                   "1003-k"
#define CONFIG_WIFI_PASSWORD               "th880511"

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61 || (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0))
#define CONFIG_WIFI_BAND_MODE   WIFI_BAND_MODE_2G_ONLY
#define CONFIG_WIFI_2G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_5G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_2G_PROTOCOL             WIFI_PROTOCOL_11N
#define CONFIG_WIFI_5G_PROTOCOL             WIFI_PROTOCOL_11N
#else
#define CONFIG_WIFI_BANDWIDTH           WIFI_BW_HT40
#endif

#define CONFIG_ESP_NOW_PHYMODE           WIFI_PHY_MODE_11G /* AP 1003-k associates as 802.11b/g — HT/MCS rates are rejected */
#define CONFIG_ESP_NOW_RATE             WIFI_PHY_RATE_6M
#define CONFIG_SEND_FREQUENCY               100

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

/* TX2 (ESP32-C3) CSI 源 MAC：本地管理地址 1a:00:00:00:00:<last>，末字节 = 本节点
 * mesh observer_id，由 Kconfig CIS_TX_NODE_2 固定派生（main/Kconfig.projbuild）。
 * 节点身份总表：0x00=node1/经典#1(TX1)、0x01=C3(TX2, 本工程)、
 * 0x02=node3/经典#2(TX3)、0x03=S3 RX —— 四节点末字节必须互异（MUST differ，
 * 否则 RX 侧按源 MAC 分链路混叠；docs/标定SOP.md §0.2, IMPLEMENTATION_PLAN.md M4） */
static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, CONFIG_CIS_TX_MAC_LAST};
static const char *TAG = "csi_send_c3";

static EventGroupHandle_t s_wifi_event_group;
static volatile bool s_sta_started = false;
static volatile bool s_sta_connect_requested = false;
#define WIFI_CONNECTED_BIT BIT0

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        s_sta_started = true;
        if (s_sta_connect_requested) {
            esp_wifi_connect();
        }
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_sta_connect_requested) {
            ESP_LOGW(TAG, "STA disconnected, retrying connect");
            esp_wifi_connect();
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "got ip:" IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

/* STA connect to the AP (router mode, IMPLEMENTATION_PLAN.md §3/§4).
 * AP is fixed on channel 6, so association also locks the PHY channel. */
static void wifi_sta_connect(void)
{
    s_wifi_event_group = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = CONFIG_WIFI_SSID,
            .password = CONFIG_WIFI_PASSWORD,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    s_sta_connect_requested = true;
    /* always connect directly: STA_START fires during esp_wifi_start() which
     * runs before these handlers are registered, so the event may be missed */
    esp_wifi_connect();

    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                                           pdFALSE, pdFALSE, pdMS_TO_TICKS(30000));
    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "connected to AP %s, STA channel follows AP", CONFIG_WIFI_SSID);
    } else {
        ESP_LOGW(TAG, "no IP from %s within 30s, continue ESP-NOW broadcast anyway", CONFIG_WIFI_SSID);
    }
}

static void wifi_init()
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    ESP_ERROR_CHECK(esp_netif_init());
    /* create the default STA netif — without it there is no DHCP client and
     * IP_EVENT_STA_GOT_IP never fires (caused a 30s connect timeout, found 2026-10-27) */
    esp_netif_t *sta_netif = esp_netif_create_default_wifi_sta();
    assert(sta_netif != NULL);
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

#if CONFIG_IDF_TARGET_ESP32C5
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
        .ghz_5g = CONFIG_WIFI_5G_PROTOCOL
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
        .ghz_5g = CONFIG_WIFI_5G_BANDWIDTHS
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#else
    ESP_ERROR_CHECK(esp_wifi_start());
    /* NOTE: no esp_wifi_set_bandwidth() on esp32 — both pre-start and post-start
     * HT40 calls return ESP_ERR_INVALID_ARG on IDF v5.5.6 (boot-looped twice on
     * hardware, 2026-10-27). STA stays at default HT20 and follows the AP. */
#endif

    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
#if CONFIG_IDF_TARGET_ESP32C5
    if ((CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20)
            || (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_5G_ONLY && CONFIG_WIFI_5G_BANDWIDTHS == WIFI_BW_HT20)) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    if (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#else
    /* esp32: no fixed channel — STA follows the AP's channel after association
     * (user instruction 2026-10-27: 信道无需硬编码) */
#endif
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
}

static void wifi_esp_now_init(esp_now_peer_info_t peer)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE,
        .ersu = false,
        .dcm = false
    };
    /* non-fatal: rate may be rejected depending on negotiated PHY (e.g. bg) —
     * sends then fall back to the default ESPNOW rate */
    esp_err_t rate_err = esp_now_set_peer_rate_config(peer.peer_addr, &rate_config);
    if (rate_err != ESP_OK) {
        ESP_LOGW(TAG, "esp_now_set_peer_rate_config failed: %s, using default rate",
                 esp_err_to_name(rate_err));
    }
}

void app_main()
{
    /**
     * @brief Initialize NVS
     */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /**
     * @brief Initialize Wi-Fi
     */
    wifi_init();

    /**
     * @brief Connect STA to the router (1003-k, STA channel follows AP)
     */
    wifi_sta_connect();

    /**
     * @brief Initialize ESP-NOW
     *        ESP-NOW protocol see: https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/network/esp_now.html
     */
    esp_now_peer_info_t peer = {
        .channel   = 0, /* 0 = follow the current STA channel (AP-determined) */
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };
    wifi_esp_now_init(peer);

    /**
     * @brief Mesh observer：CSI 直收白名单源 + 特征聚包单播 S3 + CMD_TRAIN 标定
     *        （顺序：白名单/阈值恢复 → S3 peer/打包任务 → 打开 CSI + 排窗 timer）
     */
    ESP_ERROR_CHECK(cis_csi_rx_init());
    ESP_ERROR_CHECK(cis_tx_train_init());
    ESP_ERROR_CHECK(cis_now_init());
    ESP_ERROR_CHECK(cis_csi_rx_start());

    ESP_LOGI(TAG, "================ CSI SEND + MESH OBSERVER ================");
    ESP_LOGI(TAG, "ap: %s, send_frequency: %d, mac: " MACSTR,
             CONFIG_WIFI_SSID, CONFIG_SEND_FREQUENCY, MAC2STR(CONFIG_CSI_SEND_MAC));
    ESP_LOGI(TAG, "node_id: %d (Kconfig CIS_TX_NODE_ID), observer_id/source MAC last byte: 0x%02x",
             CONFIG_CIS_TX_NODE_ID, CONFIG_CSI_SEND_MAC[5]);
    ESP_LOGI(TAG, "Role: TX2 (ESP32-C3) — fixed node2 build (choice CIS_TX_NODE_2)");
    ESP_LOGI(TAG, "CSI observed-source whitelist printed above by cis_csi_rx; "
             "100Hz source frames unchanged");

    for (uint32_t count = 0; ; ++count) {
        esp_err_t ret = esp_now_send(peer.peer_addr, (const uint8_t *)&count, sizeof(count));
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "free_heap: %ld <%s> ESP-NOW send error", esp_get_free_heap_size(), esp_err_to_name(ret));
        }

        usleep(1000 * 1000 / CONFIG_SEND_FREQUENCY);
    }
}
