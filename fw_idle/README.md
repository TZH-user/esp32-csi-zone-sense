# fw_idle — 轻载空闲固件

无 WiFi、无射频的 10s 心跳空闲固件（`main/main.c:6-12`：循环打印
`fw_idle alive, uptime_s=…`，每 10s 一次），target=esp32（`sdkconfig`
`CONFIG_IDF_TARGET="esp32"`）。

**用途**：待机板卡降温与备份在位校验 —— 烧入后串口周期心跳即证明板卡
在位、可下载、可运行。

> **角色变更**：本工程不再是经典#2 板的常驻固件。经典#2 已转为
> "收发一体"节点（发送配置=经典#2/TX3），由 `scripts/flash_tx.bat tx3`
> 以 `CONFIG_CSI_TX_NODE_ID=3` 构建 `csi_tx_v1` 烧录。fw_idle 仅作
> 空闲/降温和备份在位校验（配合 `scripts/backup_flash.sh` 做在位确认）。
