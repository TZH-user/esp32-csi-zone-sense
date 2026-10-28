/*
 * ESP32_CIS_V1.0 · app_main.c — RX 主入口（M2：检测 + 上报 + 双 REPL）
 *
 * 启动序列（实施计划 §4）：
 *   nvs_flash_init → cis_console_start（REPL 先起，任何后续步骤卡住都可现场干预）
 *   → esp_radar_init/start（注册 cis_detect 的两个回调；信道 CONFIG_CIS_DEFAULT_CHANNEL）
 *   → cis_peer_load（NVS 恢复 peer 表 + 逐个 esp_radar_new_peer）
 *   → cis_wifi_start（NVS 凭据连 STA，30s 失败 SoftAP 兜底；异步任务不阻塞）
 *   → cis_detect_start → cis_report_start
 *
 * 与计划文字顺序的差异说明：计划写 cis_wifi_start 在 esp_radar_init 之前，但
 * esp-radar 0.3.4 的 esp_radar_wifi_init 负责初始化并启动 WiFi（esp_radar.c:1633），
 * wifi 必须先由它拉起，否则重复 esp_wifi_init 会失败。本顺序在功能上等价
 * （连网为异步，不影响 CSI 直收；STA 连上后信道跟随 AP，TX 同路由 → 同信道）。
 *
 * esp-radar 配置：filter_mac = TX1 的 CSI 身份 MAC（v1.2：1a:00:00:00:00:00）。
 * 只用默认 peer 时走 esp-radar 旧过滤路径（只收 TX1，M1 已验证的轻量路径）；
 * `peer add` 注册精确 peer 后按源 MAC 精确分发（esp_radar.c esp_radar_select_peer）。
 * 不用 wildcard（FF:FF..）作默认 peer：M2 实测它会把信道 6 上所有邻居流量
 * （扫描表一屏 7+ 个 MAC）全部送进重建+检测管线，esp-radar 窗口被
 * "Timestamp delta out of range, clear window" 持续清空，cb_ex 饿死。
 * 代价：peer scan 只能看到已通过过滤的 MAC（首个=TX1 bootstrap）；其余 TX 的
 * MAC 按 §7 SOP 从 TX 开机日志抄取。
 * 检测统计/FSM/阈值全部由 cis_detect 自管，不触碰 esp_radar_motion_dec 单例。
 */

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "nvs_flash.h"
#include "esp_radar.h"

#include "cis_console.h"
#include "cis_wifi.h"
#include "cis_peer.h"
#include "cis_detect.h"
#include "cis_report.h"
#include "cis_mesh.h"

static const char *TAG = "cis_rx";

/* TX1 = csi_send 的 CONFIG_CSI_SEND_MAC（esp_wifi_set_mac 有意设置的本地管理地址，
 * 上游示例与 M1 实测一致；peer add / 过滤均用它，物理 MAC 另经 esptool 核对） */
static const uint8_t CIS_BOOTSTRAP_MAC[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};

static void cis_nvs_init(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
}

void app_main(void)
{
    cis_nvs_init();
    cis_detect_init();
    cis_console_start();          /* USB-Serial-JTAG REPL + TCP 2323 */

    esp_radar_config_t radar_config = ESP_RADAR_CONFIG_DEFAULT();
    memcpy(radar_config.csi_config.filter_mac, CIS_BOOTSTRAP_MAC, 6);  /* 默认 peer = TX1 精确过滤 */
    radar_config.wifi_config.channel = CONFIG_CIS_DEFAULT_CHANNEL;
    radar_config.wifi_config.bandwidths.ghz_2g = WIFI_BW_HT20; /* 20MHz，路由器固定 20M（§7） */
    radar_config.csi_config.csi_recv_interval = 10;            /* 10ms @ 100Hz（TX1 csi_send） */
    radar_config.csi_config.csi_filtered_cb = cis_detect_csi_cb;
    radar_config.dec_config.wifi_radar_cb_ex = cis_detect_radar_cb_ex;
    radar_config.dec_config.outliers_threshold = 0;            /* 与 M1 一致，离群过滤自管 */

    ESP_ERROR_CHECK(esp_radar_init(&radar_config));
    ESP_ERROR_CHECK(esp_radar_start());
    ESP_LOGI(TAG, "esp-radar started: channel=%d bw=HT20 filter_mac=" MACSTR,
             CONFIG_CIS_DEFAULT_CHANNEL, MAC2STR(CIS_BOOTSTRAP_MAC));

    /* ESP-NOW mesh（多节点汇聚）：设本节点源 MAC（1a:..:03，须在 WiFi 已由 esp-radar
     * 拉起后、STA connect 前）+ esp_now peer/收发任务 + 12 链路表。
     * 必须早于 cis_wifi_start（MAC 先于 connect 生效）与 cis_report_start。 */
    ESP_ERROR_CHECK(cis_mesh_start());

    cis_console_net_start();      /* TCP 2323 REPL（lwip 已由 esp-radar 拉起） */
    cis_peer_load();              /* NVS peer 表 → esp_radar_new_peer + 检测上下文 */
    cis_wifi_start();             /* NVS 凭据连 STA（异步），30s 失败 SoftAP 兜底 */
    cis_detect_start();
    cis_report_start();           /* 0x01/0x12/0x13/0x14 帧（遗留 0x10/0x11/0x30 可选） + UDP 可选 */

    ESP_LOGI(TAG, "ESP32_CIS_V1.0 RX ready (M2): detect+report+console, mode=%s",
#if defined(CONFIG_CIS_MODE_DIRECT)
             "DIRECT"
#else
             "ROUTER"
#endif
             );
}
