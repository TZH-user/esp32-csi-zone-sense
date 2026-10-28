#!/usr/bin/env bash
# backup_flash.sh -- 烧录覆盖前用 esptool read_flash 整片备份（实施计划 §9.2 红线）
# 配套 docs/标定SOP.md §3。
#
# 用法:
#   backup_flash.sh <COM口> <16M|4M> [输出目录]
# 示例:
#   backup_flash.sh COM3 16M          # RX S3#1（16MB flash）
#   backup_flash.sh COM15 4M          # 经典 ESP32 / C3（4MB flash）
#
# 输出: <输出目录>/<端口>_<芯片>_flash<容量>_<日期>.bin
#       默认输出目录 = 本脚本所在目录的 backup/（即 scripts/backup/）
# 行为:
#   1) 先 esptool chip_id 确认板在位并记录 MAC/芯片型号（自动识别 esp32/esp32s3/esp32c3）；
#   2) read_flash 0x0 起整片读出；目标文件已存在时拒绝覆盖（备份宁可重命名也不覆盖）；
#   3) 读完后校验文件字节数。
# 注意: 备份期间独占该 COM 口，其他会话不得并行监控/烧录。

set -euo pipefail

PORT="${1:-}"
SIZE_ARG="${2:-}"
OUTDIR="${3:-}"

if [[ -z "$PORT" || -z "$SIZE_ARG" ]]; then
    echo "用法: backup_flash.sh <COM口> <16M|4M> [输出目录]" >&2
    echo "示例: backup_flash.sh COM3 16M" >&2
    exit 1
fi

case "$SIZE_ARG" in
    16M) SIZE_DEC=16777216 ;;
    4M)  SIZE_DEC=4194304 ;;
    *)   echo "[ABORT] 容量仅支持 16M 或 4M（本工程板卡：S3=16M，经典 ESP32 / C3=4M）" >&2; exit 1 ;;
esac

# esptool 优先用 IDF venv 的独立可执行文件，否则退回 python -m esptool
ESPTOOL="C:/Espressif/tools/python/v5.5.6/venv/Scripts/esptool.exe"
ESPTOOL_CMD=("$ESPTOOL")
if [[ ! -x "$ESPTOOL" ]]; then
    ESPTOOL_CMD=("C:/Espressif/tools/python/v5.5.6/venv/Scripts/python.exe" -m esptool)
fi

if [[ -z "$OUTDIR" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    OUTDIR="$SCRIPT_DIR/backup"
fi
mkdir -p "$OUTDIR"

echo "[1/3] esptool chip_id 确认 $PORT 板卡在位并识别型号..."
CHIP_LOG="$(mktemp)"
# set -e: 板不在位/口被占用时 esptool 非零退出，脚本在此中止
"${ESPTOOL_CMD[@]}" --port "$PORT" chip_id 2>&1 | tee "$CHIP_LOG"

CHIP="esp32"
if grep -qi "Chip is ESP32-S3" "$CHIP_LOG"; then
    CHIP="esp32s3"
elif grep -qi "Chip is ESP32-C3" "$CHIP_LOG"; then
    CHIP="esp32c3"
elif grep -qi "Chip is ESP32-C2" "$CHIP_LOG"; then
    CHIP="esp32c2"
elif grep -qi "Chip is ESP32 " "$CHIP_LOG" || grep -qi "Chip is ESP32-D" "$CHIP_LOG"; then
    CHIP="esp32"
fi
MAC_LINE="$(grep -i "MAC:" "$CHIP_LOG" | head -1 | tr -d '\r' || true)"
echo "       识别: chip=$CHIP  ${MAC_LINE:-（未读到 MAC 行）}"
rm -f "$CHIP_LOG"

STAMP="$(date +%Y%m%d)"
OUT="$OUTDIR/${PORT}_${CHIP}_flash${SIZE_ARG}_${STAMP}.bin"
if [[ -e "$OUT" ]]; then
    echo "[ABORT] 目标备份文件已存在: $OUT" >&2
    echo "        备份文件不允许覆盖。请先改名/挪走旧文件（或用另一输出目录）再重跑。" >&2
    exit 1
fi

echo "[2/3] read_flash 0x0 起 $SIZE_DEC 字节 -> $OUT ..."
"${ESPTOOL_CMD[@]}" --port "$PORT" read_flash 0 "$SIZE_DEC" "$OUT"

echo "[3/3] 校验备份文件大小..."
ACTUAL="$(stat -c %s "$OUT" 2>/dev/null || wc -c < "$OUT" | tr -d ' ')"
if [[ "$ACTUAL" != "$SIZE_DEC" ]]; then
    echo "[FAIL] 文件大小 $ACTUAL != 期望 $SIZE_DEC，备份不完整，勿删除原固件！重试或换口/降波特率。" >&2
    exit 1
fi

echo "[DONE] 备份完成: $OUT ($ACTUAL bytes, chip=$CHIP)"
echo "       回刷命令: ${ESPTOOL_CMD[*]} --port $PORT write_flash 0x0 \"$OUT\""
ls -la "$OUT"
