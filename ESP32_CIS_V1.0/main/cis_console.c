/*
 * ESP32_CIS_V1.0 · cis_console.c — 命令 + 双 REPL（USB-Serial-JTAG / TCP 2323）
 *
 * 命令（实施计划 §4/§7 + 多节点升级 2026-10）：
 *   peer add <mac> [name] | peer del <mac> | peer list | peer scan
 *   train [observer] <mac> [seconds]（带 observer 转发对应节点；不带全网下发+本地）
 *   threshold <mac> [value] | threshold save
 *   report [on|off|rate <1~50>|status]
 *   udp <on [ip]|off|status>
 *   link list（12 链路 observer→source / 新鲜度 / seq 丢失）
 *   mesh status（espnow 收发丢包计数 + 各发送方 last_seen）
 *   dump [mac]
 * 另有移植件命令：wifi_config / wifi_scan / ping / system（restart, mem 等）。
 */
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <errno.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_console.h"
#include "esp_log_write.h"      /* esp_log_set_vprintf / vprintf_like_t */
#include "lwip/sockets.h"
#include "esp_mac.h"

#include "cis_console.h"
#include "cis_peer.h"
#include "cis_detect.h"
#include "cis_report.h"
#include "cis_mesh.h"
#include "cis_wifi.h"
#include "csi_commands.h"

static const char *TAG = "cis_console";

/* ============ 小工具 ============ */

static bool mac_parse(const char *str, uint8_t mac[6])
{
    unsigned int m[6] = {0};
    if (sscanf(str, "%x:%x:%x:%x:%x:%x",
               &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]) != 6) {
        return false;
    }
    for (int i = 0; i < 6; i++) {
        if (m[i] > 0xFF) {
            return false;
        }
        mac[i] = (uint8_t)m[i];
    }
    return true;
}

/* ============ peer 命令 ============ */

static void print_peer_list(void)
{
    int n = 0;
    for (int i = 0; i < CIS_PEER_MAX; i++) {
        const cis_peer_entry_t *e = cis_peer_get(i);
        if (!e) {
            continue;
        }
        cis_det_stats_t st;
        esp_err_t err = cis_detect_get_stats(CIS_OBSERVER_SELF, e->mac, &st);
        ESP_LOGI(TAG, "peer[%d] " MACSTR " name=%s thr=%u c state=%u var=%u mean=%u p2p=%u rssi=%d",
                 i, MAC2STR(e->mac), e->name, e->threshold,
                 err == ESP_OK ? st.state : 0xFF,
                 err == ESP_OK ? st.var_c : 0,
                 err == ESP_OK ? st.mean_c : 0,
                 err == ESP_OK ? st.p2p_c : 0,
                 err == ESP_OK ? st.rssi : 0);
        n++;
    }
    ESP_LOGI(TAG, "%d peer(s); name_id = 槽位号，0x10 帧携带", n);
}

static int cmd_peer(int argc, char **argv)
{
    if (argc < 2) {
        ESP_LOGE(TAG, "usage: peer add <mac> [name] | peer del <mac> | peer list | peer scan");
        return 1;
    }
    uint8_t mac[6];

    if (strcmp(argv[1], "list") == 0) {
        print_peer_list();
        return 0;
    }
    if (strcmp(argv[1], "scan") == 0) {
        ESP_LOGI(TAG, "recent CSI source MACs (last 5min activity, newest first):");
        int n = 0;
        for (size_t i = 0; i < 16; i++) {
            uint8_t m[6];
            int rssi = 0;
            uint32_t age = 0;
            if (!cis_peer_scan_get(i, m, &rssi, &age) || age > 300000) {
                break;
            }
            int slot = cis_peer_find(m);
            ESP_LOGI(TAG, "  " MACSTR " rssi=%d age=%ums%s", MAC2STR(m), rssi, age,
                     slot >= 0 ? " (registered)" : "");
            n++;
        }
        if (!n) {
            ESP_LOGW(TAG, "no CSI sources seen yet — check TX is sending / channel matches");
        }
        return 0;
    }
    if (strcmp(argv[1], "add") == 0) {
        if (argc < 3 || !mac_parse(argv[2], mac)) {
            ESP_LOGE(TAG, "usage: peer add <mac xx:xx:xx:xx:xx:xx> [name]");
            return 1;
        }
        esp_err_t err = cis_peer_add(mac, argc >= 4 ? argv[3] : NULL);
        if (err == ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "peer already exists");
            return 1;
        }
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "peer add failed: %s", esp_err_to_name(err));
            return 1;
        }
        ESP_LOGI(TAG, "OK: peer added (NVS saved)");
        return 0;
    }
    if (strcmp(argv[1], "del") == 0) {
        if (argc < 3 || !mac_parse(argv[2], mac)) {
            ESP_LOGE(TAG, "usage: peer del <mac>");
            return 1;
        }
        esp_err_t err = cis_peer_del(mac);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "peer del failed: %s", esp_err_to_name(err));
            return 1;
        }
        ESP_LOGI(TAG, "OK: peer deleted (NVS saved)");
        return 0;
    }
    ESP_LOGE(TAG, "unknown subcommand: %s", argv[1]);
    return 1;
}

/* ============ train 命令 ============ */

static int cmd_train(int argc, char **argv)
{
    if (argc < 2) {
        ESP_LOGE(TAG, "usage: train [observer] <mac> [seconds=%d]", CONFIG_CIS_TRAIN_SECONDS);
        return 1;
    }
    uint8_t mac[6];
    uint8_t observer = 0xFF;    /* 0xFF = 未指定（全网下发） */
    int mac_idx = 1;
    /* `train [observer] <mac> [sec]`：argv[1] 为 0~3 单个数字且后随 mac 时视为 observer */
    if (argc >= 3 && argv[1][1] == '\0' && argv[1][0] >= '0' && argv[1][0] <= '3') {
        observer = (uint8_t)(argv[1][0] - '0');
        mac_idx = 2;
    }
    if (!mac_parse(argv[mac_idx], mac)) {
        ESP_LOGE(TAG, "bad mac format: %s", argv[mac_idx]);
        return 1;
    }
    uint16_t sec = 0;
    if (argc >= mac_idx + 2) {
        sec = (uint16_t)atoi(argv[mac_idx + 1]);
    }

    if (observer != 0xFF) {
        esp_err_t err = cis_mesh_train(observer, mac, sec);
        if (err == ESP_ERR_NOT_FOUND) {
            ESP_LOGE(TAG, "peer not registered — run `peer add` first");
            return 1;
        }
        if (err == ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "training already in progress for this peer");
            return 1;
        }
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "train dispatch to observer %u failed: %s",
                     observer, esp_err_to_name(err));
            return 1;
        }
        ESP_LOGI(TAG, "training dispatched to observer %u; 0x14 frame will follow when done",
                 observer);
        return 0;
    }

    /* 不带 observer：4 个 observer 全下发（其余经 mesh CMD_TRAIN，本地走检测上下文） */
    esp_err_t err = ESP_OK;
    for (uint8_t obs = 0; obs <= 3; obs++) {
        esp_err_t e = cis_mesh_train(obs, mac, sec);
        if (e != ESP_OK) {
            ESP_LOGW(TAG, "observer %u: %s", obs, esp_err_to_name(e));
            err = err == ESP_OK ? e : err;
        }
    }
    if (err != ESP_OK) {
        return 1;
    }
    ESP_LOGI(TAG, "training dispatched to all 4 observers; 0x14 frames will follow");
    return 0;
}

/* ============ threshold 命令 ============ */

static int cmd_threshold(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "save") == 0) {
        esp_err_t err = cis_peer_save();
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "save failed: %s", esp_err_to_name(err));
            return 1;
        }
        ESP_LOGI(TAG, "OK: thresholds persisted to NVS");
        return 0;
    }
    if (argc < 2) {
        ESP_LOGE(TAG, "usage: threshold <mac> [value_centi_var] | threshold save");
        return 1;
    }
    uint8_t mac[6];
    if (!mac_parse(argv[1], mac)) {
        ESP_LOGE(TAG, "bad mac format: %s", argv[1]);
        return 1;
    }
    if (argc >= 3) {
        uint32_t val = (uint32_t)strtoul(argv[2], NULL, 0);
        esp_err_t err = cis_peer_set_threshold(mac, (uint16_t)val);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "set failed: %s (peer registered?)", esp_err_to_name(err));
            return 1;
        }
        ESP_LOGI(TAG, "OK: threshold=%u c (RAM; use `threshold save` to persist)", (unsigned)val);
        return 0;
    }
    /* 查询 */
    cis_det_stats_t st;
    if (cis_detect_get_stats(CIS_OBSERVER_SELF, mac, &st) != ESP_OK) {
        ESP_LOGE(TAG, "peer not registered");
        return 1;
    }
    ESP_LOGI(TAG, MACSTR " threshold=%u c (state=%u var=%u)", MAC2STR(mac),
             st.threshold, st.state, st.var_c);
    return 0;
}

/* ============ report 命令 ============ */

static int cmd_report(int argc, char **argv)
{
    if (argc < 2) {
        ESP_LOGI(TAG, "report: enable=%s rate=%uHz dropped=%u",
                 cis_report_get_enable() ? "on" : "off",
                 cis_report_get_rate(), (unsigned)cis_report_dropped());
        return 0;
    }
    if (strcmp(argv[1], "on") == 0) {
        cis_report_set_enable(true);
        ESP_LOGI(TAG, "report ON");
    } else if (strcmp(argv[1], "off") == 0) {
        cis_report_set_enable(false);
        ESP_LOGI(TAG, "report OFF (心跳/特征流暂停)");
    } else if (strcmp(argv[1], "rate") == 0 && argc >= 3) {
        cis_report_set_rate((uint8_t)atoi(argv[2]));
        ESP_LOGI(TAG, "feature rate = %u Hz", cis_report_get_rate());
    } else {
        ESP_LOGE(TAG, "usage: report [on|off|rate <1~50>]");
        return 1;
    }
    return 0;
}

/* ============ udp 命令 ============ */

static int cmd_udp(int argc, char **argv)
{
    if (argc < 2) {
        char st[96];
        cis_report_udp_status(st, sizeof(st));
        ESP_LOGI(TAG, "%s", st);
        return 0;
    }
    if (strcmp(argv[1], "on") == 0) {
        const char *ip = (argc >= 3) ? argv[2] : NULL;
        esp_err_t err = cis_report_udp_set(true, ip);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "bad ip: %s", ip ? ip : "?");
            return 1;
        }
        char st[96];
        cis_report_udp_status(st, sizeof(st));
        ESP_LOGI(TAG, "udp ON: %s", st);
    } else if (strcmp(argv[1], "off") == 0) {
        cis_report_udp_set(false, NULL);
        ESP_LOGI(TAG, "udp OFF");
    } else {
        ESP_LOGE(TAG, "usage: udp <on [target_ip]> | off");
        return 1;
    }
    return 0;
}

/* ============ dump 命令 ============ */

static int cmd_dump(int argc, char **argv)
{
    if (argc >= 2) {
        uint8_t mac[6];
        if (!mac_parse(argv[1], mac)) {
            ESP_LOGE(TAG, "bad mac format: %s", argv[1]);
            return 1;
        }
        cis_det_stats_t st;
        if (cis_detect_get_stats(CIS_OBSERVER_SELF, mac, &st) != ESP_OK) {
            ESP_LOGE(TAG, "peer not registered");
            return 1;
        }
        ESP_LOGI(TAG, MACSTR " state=%u thr=%u mean=%u var=%u p2p=%u rssi=%d "
                 "windows=%u dirty=%u training=%s left=%us ts=%ums",
                 MAC2STR(mac), st.state, st.threshold, st.mean_c, st.var_c, st.p2p_c,
                 st.rssi, (unsigned)st.windows, (unsigned)st.dirty_windows,
                 st.training ? "yes" : "no", st.train_left_s, (unsigned)st.ts_ms);
        return 0;
    }
    print_peer_list();
    char st[96];
    cis_report_udp_status(st, sizeof(st));
    ESP_LOGI(TAG, "report enable=%s rate=%uHz dropped=%u lost(det)=%u | %s",
             cis_report_get_enable() ? "on" : "off", cis_report_get_rate(),
             (unsigned)cis_report_dropped(), (unsigned)cis_detect_lost_frames(), st);
    char ms[128];
    cis_mesh_status(ms, sizeof(ms));
    ESP_LOGI(TAG, "%s", ms);
    return 0;
}

/* ============ link / mesh 命令（多节点） ============ */

static int cmd_link(int argc, char **argv)
{
    if (argc < 2 || strcmp(argv[1], "list") != 0) {
        ESP_LOGE(TAG, "usage: link list");
        return 1;
    }
    uint32_t stale_ms = 2u * 1000u / CONFIG_CIS_MESH_FEATURE_RATE_HZ;
    ESP_LOGI(TAG, "mesh links (12 = 4 observer x 3 source; self=%u; ts 为各节点本地 uptime)",
             CIS_OBSERVER_SELF);
    for (size_t i = 0; i < CIS_LINK_MAX; i++) {
        cis_mesh_link_info_t info;
        if (!cis_mesh_link_get(i, &info)) {
            break;
        }
        const char *fresh = (info.active && info.age_ms < stale_ms) ? "" : " (stale)";
        ESP_LOGI(TAG, "link[%2u] %u->" MACSTR " %s thr=%u var=%lu mean=%lu p2p=%lu rssi=%d "
                 "age=%lums seq_lost=%lu%s",
                 (unsigned)i, info.observer, MAC2STR(info.mac),
                 info.active ? (info.registered ? "loc" : "rem") : "---",
                 info.threshold,
                 (unsigned long)info.var_c, (unsigned long)info.mean_c,
                 (unsigned long)info.p2p_c, info.rssi,
                 (unsigned long)info.age_ms, (unsigned long)info.seq_lost, fresh);
    }
    return 0;
}

static int cmd_mesh(int argc, char **argv)
{
    if (argc < 2 || strcmp(argv[1], "status") != 0) {
        ESP_LOGE(TAG, "usage: mesh status");
        return 1;
    }
    char st[128];
    cis_mesh_status(st, sizeof(st));
    ESP_LOGI(TAG, "%s", st);
    for (size_t i = 0; i < 4; i++) {
        uint8_t obs = 0;
        bool seen = false;
        uint32_t age = 0, pkts = 0, lost = 0;
        if (!cis_mesh_node_get(i, &obs, &seen, &age, &pkts, &lost)) {
            break;
        }
        if (!seen) {
            ESP_LOGI(TAG, "node %u: no data yet", obs);
        } else {
            ESP_LOGI(TAG, "node %u: pkts=%lu lost=%lu (%lu.%lu%%) last_seen=%lums ago",
                     obs, (unsigned long)pkts, (unsigned long)lost,
                     (unsigned long)(pkts ? lost * 100 / pkts : 0),
                     (unsigned long)(pkts ? lost * 100 % pkts * 10 / pkts : 0),
                     (unsigned long)age);
        }
    }
    return 0;
}

/* ============ wifi_status 命令 ============ */

static int cmd_wifi_status(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    char st[128];
    cis_wifi_status(st, sizeof(st));
    ESP_LOGI(TAG, "%s", st);
    return 0;
}

/* ============ TCP 2323 控制台 ============ */

#define TCP_CONSOLE_PORT  CONFIG_CIS_TCP_CONSOLE_PORT
#define TCP_LINE_MAX      256

static vprintf_like_t s_saved_vprintf;
static volatile int s_tcp_fd = -1;

static int tcp_log_vprintf(const char *fmt, va_list args)
{
    int ret = 0;
    if (s_saved_vprintf) {
        ret = s_saved_vprintf(fmt, args);    /* 串口照常输出 */
    }
    int fd = s_tcp_fd;
    if (fd >= 0) {
        char buf[384];
        va_list copy;
        va_copy(copy, args);
        int n = vsnprintf(buf, sizeof(buf), fmt, copy);
        va_end(copy);
        if (n > 0) {
            if (n > (int)sizeof(buf)) {
                n = sizeof(buf);
            }
            int w = send(fd, buf, n, 0);
            (void)w;    /* 客户端断开时静默丢弃 */
        }
    }
    return ret;
}

static void tcp_console_task(void *arg)
{
    (void)arg;
    int listen_sock = -1;

    struct sockaddr_in bind_addr = {
        .sin_family = AF_INET,
        .sin_port = htons(TCP_CONSOLE_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };

    for (;;) {
        if (listen_sock < 0) {
            listen_sock = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
            if (listen_sock < 0) {
                ESP_LOGE(TAG, "tcp console: socket failed");
                vTaskDelay(pdMS_TO_TICKS(5000));
                continue;
            }
            int reuse = 1;
            setsockopt(listen_sock, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
            if (bind(listen_sock, (struct sockaddr *)&bind_addr, sizeof(bind_addr)) != 0 ||
                    listen(listen_sock, 1) != 0) {
                ESP_LOGE(TAG, "tcp console: bind/listen port %d failed", TCP_CONSOLE_PORT);
                close(listen_sock);
                listen_sock = -1;
                vTaskDelay(pdMS_TO_TICKS(5000));
                continue;
            }
            ESP_LOGI(TAG, "tcp console listening on port %d", TCP_CONSOLE_PORT);
        }

        struct sockaddr_in client_addr;
        socklen_t addr_len = sizeof(client_addr);
        int client = accept(listen_sock, (struct sockaddr *)&client_addr, &addr_len);
        if (client < 0) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        if (s_tcp_fd >= 0) {
            const char *busy = "another client is using the console\n";
            send(client, busy, strlen(busy), 0);
            close(client);
            continue;
        }
        s_tcp_fd = client;
        struct timeval tv = { .tv_sec = 300, .tv_usec = 0 };
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

        const char *banner = "ESP32_CIS console (TCP). type `help`-commands over serial; "
                             "cis commands: peer/train/threshold/report/udp/dump/wifi_status\n"
                             "cis> ";
        send(client, banner, strlen(banner), 0);

        char line[TCP_LINE_MAX];
        size_t len = 0;
        bool run = true;
        while (run) {
            char c;
            int n = recv(client, &c, 1, 0);
            if (n <= 0) {
                run = false;    /* 断开或超时 */
                break;
            }
            if (c == '\n' || c == '\r') {
                if (len == 0) {
                    send(client, "cis> ", 5, 0);
                    continue;
                }
                line[len] = '\0';
                ESP_LOGD(TAG, "tcp cmd: %s", line);
                s_saved_vprintf = esp_log_set_vprintf(tcp_log_vprintf);
                int ret = 0;
                esp_err_t err = esp_console_run(line, &ret);
                esp_log_set_vprintf(s_saved_vprintf);
                if (err != ESP_OK) {
                    char msg[64];
                    int m = snprintf(msg, sizeof(msg), "command error: %s\n",
                                     err == ESP_ERR_INVALID_ARG ? "not found" : esp_err_to_name(err));
                    send(client, msg, m, 0);
                } else {
                    char msg[32];
                    int m = snprintf(msg, sizeof(msg), "ret=%d\n", ret);
                    send(client, msg, m, 0);
                }
                send(client, "cis> ", 5, 0);
                len = 0;
                continue;
            }
            if (len < TCP_LINE_MAX - 1) {
                line[len++] = c;
            }
        }
        s_tcp_fd = -1;
        close(client);
        ESP_LOGI(TAG, "tcp console client disconnected");
    }
}

/* ============ 启动 ============ */

void cis_console_start(void)
{
    /* 本模块命令 */
    const esp_console_cmd_t cmds[] = {
        { .command = "peer",      .help = "peer add <mac> [name] | del <mac> | list | scan",
          .func = &cmd_peer },
        { .command = "train",     .help = "train [observer] <mac> [seconds] — 静默标定，结束回 0x14（0~3=指定节点转发，缺省全网下发+本地）",
          .func = &cmd_train },
        { .command = "threshold", .help = "threshold <mac> [val] | save",
          .func = &cmd_threshold },
        { .command = "report",    .help = "report [on|off|rate <1~50>]",
          .func = &cmd_report },
        { .command = "udp",       .help = "udp <on [ip]|off|status> — UDP 3333 上报",
          .func = &cmd_udp },
        { .command = "link",      .help = "link list — 12 链路 observer→source / 新鲜度 / seq 丢失",
          .func = &cmd_link },
        { .command = "mesh",      .help = "mesh status — espnow 收发/丢包计数 + 各发送方 last_seen",
          .func = &cmd_mesh },
        { .command = "dump",      .help = "dump [mac] — 实时统计",
          .func = &cmd_dump },
        { .command = "wifi_status", .help = "wifi 连接状态",
          .func = &cmd_wifi_status },
    };
    for (size_t i = 0; i < sizeof(cmds) / sizeof(cmds[0]); i++) {
        ESP_ERROR_CHECK(esp_console_cmd_register(&cmds[i]));
    }
    /* 移植件命令 */
    cmd_register_ping();
    cmd_register_system();
    cmd_register_wifi_config();
    cmd_register_wifi_scan();

    /* REPL 1：USB-Serial-JTAG（COM3，M1 已验证路径） */
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t repl_config = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    repl_config.prompt = "cis>";
    esp_console_dev_usb_serial_jtag_config_t usbj_config = ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&usbj_config, &repl_config, &repl));
    ESP_ERROR_CHECK(esp_console_start_repl(repl));
}

void cis_console_net_start(void)
{
    /* REPL 2：TCP 2323。必须在 esp_radar_init 之后调用——lwip 由 esp-radar 的
     * esp_radar_wifi_init（esp_netif_init）拉起，提前 socket() 会触发
     * tcpip.c:454 "Invalid mbox" 断言（M2 实测踩坑）。 */
    if (xTaskCreate(tcp_console_task, "cis_tcp", 6144, NULL, 3, NULL) != pdPASS) {
        ESP_LOGE(TAG, "create tcp console task failed");
    }
}
