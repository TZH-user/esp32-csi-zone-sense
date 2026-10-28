/*
 * ESP32_CIS_V1.0 · cis_console.h — 控制台：命令注册 + 双 REPL
 *
 * REPL 1：esp_console_new_repl_usb_serial_jtag（COM3，主 console，M1 已验证）。
 * REPL 2：TCP 2323（无线标定）。IDF v5.5.6 的 esp_console 没有 esp_console_new_repl_net
 * （实施计划 §4 所引 API 在本 IDF 中不存在），故自实现：TCP 任务 accept → 逐行
 * esp_console_run()；命令执行期间用 esp_log_set_vprintf 把 ESP_LOG 输出同时镜像到
 * socket（串口输出保留）。限制：命令的 printf 直写 stdout（如 `help`）只在串口可见，
 * 本模块命令统一用 ESP_LOGI 输出，TCP 客户端可见。
 */
#pragma once

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 注册全部 CIS 命令并启动 USB-Serial-JTAG REPL（COM3，主 console）。
 *  可在 lwip 就绪前调用。 */
void cis_console_start(void);

/** 启动 TCP 2323 控制台任务。必须在 esp_radar_init() 之后调用
 *  （lwip 由 esp-radar 的 esp_radar_wifi_init 拉起）。 */
void cis_console_net_start(void);

#ifdef __cplusplus
}
#endif
