#include <stdio.h>
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/* 轻载空闲固件：无 WiFi、无射频、10s 心跳一次，给等待任务的板卡降温用 */
void app_main(void)
{
    while (1) {
        printf("fw_idle alive, uptime_s=%ld\n", (long)(esp_timer_get_time() / 1000000ULL));
        vTaskDelay(pdMS_TO_TICKS(10000));
    }
}
