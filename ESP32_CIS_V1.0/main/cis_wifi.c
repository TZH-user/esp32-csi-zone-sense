/*
 * ESP32_CIS_V1.0 · cis_wifi.c — STA 连网 + NVS 凭据 + 重连 + SoftAP 兜底
 */
#include <string.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_mac.h"
#include "nvs.h"
#include "lwip/ip_addr.h"

#include "cis_wifi.h"
#include "cis_protocol.h"   /* cis_self_source_mac（多节点 observer 身份） */

static const char *TAG = "cis_wifi";

#define WIFI_CONNECTED_BIT BIT0
#define SOFTAP_FALLBACK_MS (30 * 1000)

static EventGroupHandle_t s_event_group;
static volatile bool s_sta_connected;
static volatile bool s_ap_mode;
static volatile bool s_reconnect = true;
static char s_ssid[33];
static char s_pass[65];

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_sta_connected = false;
        xEventGroupClearBits(s_event_group, WIFI_CONNECTED_BIT);
        if (s_reconnect && !s_ap_mode) {
            ESP_LOGW(TAG, "sta disconnected, reconnecting...");
            esp_wifi_connect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *evt = (ip_event_got_ip_t *)data;
        s_sta_connected = true;
        xEventGroupSetBits(s_event_group, WIFI_CONNECTED_BIT);
        uint8_t prim = 0;
        wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
        esp_wifi_get_channel(&prim, &second);
        ESP_LOGI(TAG, "sta got IP: " IPSTR " (channel=%u)", IP2STR(&evt->ip_info.ip), prim);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STACONNECTED) {
        ESP_LOGI(TAG, "softap: station connected");
    }
}

static bool load_creds(void)
{
    size_t len = sizeof(s_ssid);
    nvs_handle_t h;
    if (nvs_open(CIS_WIFI_NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    esp_err_t e1 = nvs_get_str(h, CIS_WIFI_NVS_SSID, s_ssid, &len);
    len = sizeof(s_pass);
    esp_err_t e2 = nvs_get_str(h, CIS_WIFI_NVS_PASS, s_pass, &len);
    nvs_close(h);
    return e1 == ESP_OK && e2 == ESP_OK && s_ssid[0];
}

static void start_softap(void)
{
    esp_netif_t *ap_netif = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
    if (!ap_netif) {
        ap_netif = esp_netif_create_default_wifi_ap();
    }
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    char ap_ssid[32];
    snprintf(ap_ssid, sizeof(ap_ssid), "CIS_RX-%02x%02x", mac[4], mac[5]);

    wifi_config_t ap_config = { 0 };
    strlcpy((char *)ap_config.ap.ssid, ap_ssid, sizeof(ap_config.ap.ssid));
    ap_config.ap.ssid_len = strlen(ap_ssid);
    strlcpy((char *)ap_config.ap.password, "cis12345", sizeof(ap_config.ap.password));
    ap_config.ap.authmode = WIFI_AUTH_WPA2_PSK;
    ap_config.ap.max_connection = 4;
    ap_config.ap.channel = CONFIG_CIS_DEFAULT_CHANNEL;  /* 关键：SoftAP 不挪走 CSI 接收信道 */

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_config));
    s_ap_mode = true;
    ESP_LOGW(TAG, "SoftAP fallback ON: ssid=%s pass=cis12345 channel=%d "
             "(TCP console 192.168.4.1:%d)", ap_ssid, CONFIG_CIS_DEFAULT_CHANNEL,
             CONFIG_CIS_TCP_CONSOLE_PORT);
}

static void wifi_task(void *arg)
{
    (void)arg;

    if (load_creds()) {
        wifi_config_t sta_config = { 0 };
        strlcpy((char *)sta_config.sta.ssid, s_ssid, sizeof(sta_config.sta.ssid));
        strlcpy((char *)sta_config.sta.password, s_pass, sizeof(sta_config.sta.password));
        sta_config.sta.scan_method = WIFI_FAST_SCAN;
        ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta_config));
        /* 多节点身份：STA connect 前设本节点 CSI 源 MAC 1a:00:00:00:00:0x（末字节 =
         * observer_id）。WiFi 已由 esp-radar 拉起（esp_wifi_set_mac 可在启动后调用，
         * 同 TX 板 csi_send），DIRECT 模式不走此路径、由 cis_mesh_start 设置。 */
        uint8_t self_mac[6];
        cis_self_source_mac(self_mac);
        ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, self_mac));
        ESP_LOGI(TAG, "STA source MAC set to " MACSTR " (observer_id=0x%02x)",
                 MAC2STR(self_mac), self_mac[5]);
        ESP_LOGI(TAG, "NVS creds found (ssid=%s), connecting...", s_ssid);
        esp_wifi_connect();
    } else {
        ESP_LOGW(TAG, "no NVS credentials — run `wifi_config -s <ssid> -p <pass>`; "
                 "SoftAP fallback in %ds", SOFTAP_FALLBACK_MS / 1000);
    }

    /* 30s 内未获 IP → SoftAP 兜底（直收模式跳过 STA，仅兜底配网） */
    EventBits_t bits = xEventGroupWaitBits(s_event_group, WIFI_CONNECTED_BIT,
                                           pdFALSE, pdTRUE, pdMS_TO_TICKS(SOFTAP_FALLBACK_MS));
    if (!(bits & WIFI_CONNECTED_BIT)) {
        start_softap();
    }
    vTaskDelete(NULL);
}

void cis_wifi_start(void)
{
    if (!s_event_group) {
        s_event_group = xEventGroupCreate();
        ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                   wifi_event_handler, NULL));
        ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                   wifi_event_handler, NULL));
    }

#if CONFIG_CIS_MODE_DIRECT
    ESP_LOGI(TAG, "direct mode: fixed channel %d, no STA join", CONFIG_CIS_DEFAULT_CHANNEL);
    /* esp_radar_init 已把信道设为 CONFIG_CIS_DEFAULT_CHANNEL，这里无需再动 */
#endif

    if (xTaskCreate(wifi_task, "cis_wifi", 4096, NULL, 4, NULL) != pdPASS) {
        ESP_LOGE(TAG, "create wifi task failed");
    }
}

bool cis_wifi_connected(void)
{
    return s_sta_connected;
}

void cis_wifi_status(char *out, size_t len)
{
    if (s_sta_connected) {
        esp_netif_t *sta = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        esp_netif_ip_info_t ip = {0};
        if (sta) {
            esp_netif_get_ip_info(sta, &ip);
        }
        snprintf(out, len, "sta: connected ssid=%s ip=" IPSTR, s_ssid, IP2STR(&ip.ip));
    } else if (s_ap_mode) {
        snprintf(out, len, "softap: CIS_RX-* pass=cis12345 (sta idle, no creds or connect failed)");
    } else {
        snprintf(out, len, "sta: connecting... ssid=%s", s_ssid[0] ? s_ssid : "(none stored)");
    }
}
