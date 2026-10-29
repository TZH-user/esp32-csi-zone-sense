"""夜间无人值守采集器 —— 无界面直连 S3 网关串口录制（与 GUI Store 同款 CSV）。

用途：实验室空场窗口录「无人静置基线」——验收用例①（静置事件表应为空）的证据，
顺带攒 8 小时级尖峰统计与噪声底漂移数据。

用法：
    python overnight_collect.py --test-seconds 45     # 试跑
    python overnight_collect.py                       # 正式：默认录到 08:00 自停

落盘（data/YYYYMMDD_HHMM-overnight/）：
    cis_log.csv     与 GUI 完全同格式（store.Store 直写，analysis/回放可直接读）
    raw_serial.bin  原始串口字节留底（法证级，可离线重放）
    status.log      每 60s 一行进程心跳（行数/C盘寸/EVENT 数），崩溃留痕
    summary.log     首个 5min 小结 + 每小时小结（行数/链路/坏CRC/重连/EVENT 数）
    events.log      每个 EVENT 帧即时落一份（空场出现 EVENT = 误报，直接翻这里）

找 S3 口：优先 COM3/COM5/COM20/COM21，跳过 CIS_SCAN_SKIP 名单与蓝牙口；
COM19（鲁班猫）无论环境变量是否设置都强制跳过。
判据：收到 0x12 多节点特征帧（只有 S3 网关会发，正常 <1s 内出现）；
兜底：banner 文本匹配 S3 MAC（DC:DA:0C:1D:AE:10，分隔符不限）。

健壮性：60s 无任何字节 → 关闭重连（USB 抖动/板卡重启自愈）；串口异常 →
重新找口重试，同一 CSV 续写（断档从 ts 列可见）。打开端口会拉 DTR/RTS
复位板卡，属预期行为（空场复位无害，阈值在 NVS 不丢）。
"""

from __future__ import annotations

import argparse
import os
import re
import time
from datetime import datetime, time as dtime
from pathlib import Path

import serial

import protocol
import store as store_mod

BAUD = 2_000_000
S3_MAC_RE = re.compile(rb"DC.DA.0C.1D.AE.10", re.I)   # 分隔符不限（: 或 -）
PREFERRED = ["COM3", "COM5", "COM20", "COM21"]        # 上次会话的口序，命中即停
NAME_BY_MAC = {
    "1a:00:00:00:00:00": "经典#1",
    "1a:00:00:00:00:01": "C3",
    "1a:00:00:00:00:02": "经典#2",
    "1a:00:00:00:00:03": "S3",
}
PROBE_SECONDS = 4.0          # 打开到判定的时间窗（覆盖复位后 boot 日志期）
SILENCE_RECONNECT_S = 60.0   # 连续无字节阈值
STATUS_EVERY_S = 60.0
FIRST_SUMMARY_S = 300.0      # 开录 5 分钟出首个小结（暖机 sanity check）
SUMMARY_EVERY_S = 3600.0


def log(msg: str) -> None:
    print("[%s] %s" % (datetime.now().isoformat(timespec="seconds"), msg), flush=True)


def skip_ports() -> set:
    skip = {"COM19"}   # 鲁班猫调试口，双保险：环境变量忘了也绝不碰
    env = os.environ.get("CIS_SCAN_SKIP", "")
    skip |= {p.strip().upper() for p in env.split(",") if p.strip()}
    return skip


def candidate_ports() -> list:
    import serial.tools.list_ports as lp
    skip = skip_ports()
    found = []
    for p in lp.comports():
        desc = p.description or ""
        if p.device.upper() in skip:
            continue
        if "蓝牙" in desc or "Bluetooth" in desc.lower():
            continue
        found.append(p.device)
    return [c for c in PREFERRED if c in found] + [c for c in found if c not in PREFERRED]


def probe_s3(dev: str) -> tuple:
    """打开 dev 探测 PROBE_SECONDS：见 0x12 帧（或 banner MAC）判为 S3 网关。
    返回 (is_s3, 说明)。打不开/不是 S3 → (False, 原因)。"""
    try:
        ser = serial.Serial(dev, BAUD, timeout=0.1)
    except Exception as e:
        return False, "open fail: %s" % e
    parser = protocol.StreamParser()
    head = bytearray()          # banner 在流的开头，只留头部做 MAC 匹配
    deadline = time.time() + PROBE_SECONDS
    hit = ""
    try:
        while time.time() < deadline:
            chunk = ser.read(4096)
            if not chunk:
                continue
            if len(head) < 8192:
                head.extend(chunk)
            for fr in parser.feed(chunk):
                if fr.type == protocol.TYPE_FEATURE_MULTI:
                    hit = "0x12 特征帧 %d 帧" % parser.frames_ok
                    return True, hit
    except Exception as e:
        return False, "read fail: %s" % e
    finally:
        ser.close()
    if S3_MAC_RE.search(bytes(head)):
        return True, "banner MAC 匹配"
    return False, "无 0x12 帧（%d ok / %d dropped 字节）" % (
        parser.frames_ok, parser.bytes_dropped)


def find_s3_port() -> tuple:
    for dev in candidate_ports():
        ok, why = probe_s3(dev)
        log("probe %s -> %s%s" % (dev, "S3✓" if ok else "×", "（%s）" % why))
        if ok:
            return dev
    return None


def name_lookup(mac: str, name_id: int) -> str:
    return NAME_BY_MAC.get(mac) or store_mod.default_zone_name(mac)


def parse_stop_at(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def main() -> None:
    ap = argparse.ArgumentParser(description="夜间无人值守 CSI 采集")
    ap.add_argument("--stop-at", default="08:00", help="到点自停（默认 08:00）")
    ap.add_argument("--max-hours", type=float, default=5.5,
                    help="保险丝：无论到不到点，录满即停")
    ap.add_argument("--test-seconds", type=float, default=0,
                    help=">0 时为试跑模式：录这么久就停")
    ap.add_argument("--session-name", default="overnight")
    args = ap.parse_args()
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    session = store_mod.Store()
    csv_path = session.start_session(args.session_name)
    sdir = session.session_dir
    log("session=%s csv=%s" % (sdir.name, csv_path))
    raw_fh = open(sdir / "raw_serial.bin", "wb")
    status_fh = open(sdir / "status.log", "a", encoding="utf-8", buffering=1)
    summary_fh = open(sdir / "summary.log", "a", encoding="utf-8", buffering=1)
    events_fh = open(sdir / "events.log", "a", encoding="utf-8", buffering=1)

    started = time.time()
    stop_t = parse_stop_at(args.stop_at)
    max_s = args.max_hours * 3600

    def time_up() -> bool:
        if args.test_seconds and time.time() - started >= args.test_seconds:
            return True
        now = datetime.now()
        if (now - datetime.fromtimestamp(started)).total_seconds() > 60 \
                and now.time() >= stop_t:
            return True      # 起录 1 分钟后才生效，防止 08:00 后手动重启秒停
        return time.time() - started >= max_s

    st = {"frames_ok": 0, "bad_payload": 0, "reconnects": 0, "events": 0,
          "heartbeats": 0, "features": 0}
    per_link = {}            # "obs>mac" -> [rows, var_max]（自上次小结以来）
    parser = protocol.StreamParser()
    ser = None
    last_byte = time.time()
    last_status = time.time()
    next_summary = started + FIRST_SUMMARY_S

    def write_summary(final: bool = False) -> None:
        dur = time.time() - started
        links = sorted(per_link)
        lines = ["%s %s 时长=%.1fmin 行=%d 帧=%d 坏payload=%d 重连=%d "
                 "EVENT=%d HEARTBEAT=%d FEATURES=%d 链路=%d" % (
                     "FINAL" if final else "SUMM", datetime.now().isoformat(timespec="seconds"),
                     dur / 60, session.rows_written, st["frames_ok"], st["bad_payload"],
                     st["reconnects"], st["events"], st["heartbeats"], st["features"],
                     len(links))]
        for k in links:
            rows, vmax = per_link[k]
            lines.append("  %s rows=%d var_max=%d" % (k, rows, vmax))
        lines.append("  parser=%s" % parser.stats())
        text = "\n".join(lines) + "\n"
        summary_fh.write(text)
        print(text, flush=True)
        per_link.clear()

    try:
        while not time_up():
            now = time.time()
            if ser is None:
                dev = find_s3_port()
                if dev is None:
                    log("未找到 S3 口，5s 后重试")
                    time.sleep(5)
                    continue
                ser = serial.Serial(dev, BAUD, timeout=0.1)
                log("已连接 %s" % dev)
                last_byte = now
            chunk = ser.read(4096)
            now = time.time()
            if chunk:
                last_byte = now
                raw_fh.write(chunk)
                for fr in parser.feed(chunk):
                    rec = store_mod.frame_to_record(fr, name_lookup=name_lookup)
                    if rec is None:
                        st["bad_payload"] += 1
                        continue
                    st["frames_ok"] += 1
                    if rec.kind == "FEATURES":
                        st["features"] += 1
                        k = store_mod.link_key(rec.observer, rec.mac)
                        e = per_link.setdefault(k, [0, 0])
                        e[0] += 1
                        if rec.var is not None and rec.var > e[1]:
                            e[1] = rec.var
                    elif rec.kind == "EVENT":
                        st["events"] += 1
                        events_fh.write("%s %s>%s state=%s score=%s rssi=%s\n" % (
                            rec.ts_iso, rec.observer, rec.mac,
                            rec.state_name(), rec.score, rec.rssi))
                        log("!! EVENT（空场误报）: %s %s>%s %s" % (
                            rec.ts_iso, rec.observer, rec.mac, rec.state_name()))
                    elif rec.kind == "HEARTBEAT":
                        st["heartbeats"] += 1
                    session.record(rec)
            else:
                if now - last_byte > SILENCE_RECONNECT_S:
                    log("60s 无字节，重连（累计 %d）" % (st["reconnects"] + 1))
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                    parser = protocol.StreamParser()   # 重连后从头同步
                    st["reconnects"] += 1
                    continue
            if now - last_status >= STATUS_EVERY_S:
                raw_fh.flush()
                status_fh.write("%s rows=%d events=%d csv=%.1fMB\n" % (
                    datetime.now().isoformat(timespec="seconds"),
                    session.rows_written, st["events"],
                    csv_path.stat().st_size / 1e6))
                last_status = now
            if now >= next_summary:
                write_summary()
                next_summary += SUMMARY_EVERY_S
    except KeyboardInterrupt:
        log("Ctrl+C，收工")
    finally:
        write_summary(final=True)
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        session.stop_session()
        for fh in (raw_fh, status_fh, summary_fh, events_fh):
            try:
                fh.close()
            except Exception:
                pass
        status_fh = open(sdir / "status.log", "a", encoding="utf-8")
        status_fh.write("%s STOPPED rows=%d events=%d\n" % (
            datetime.now().isoformat(timespec="seconds"),
            session.rows_written, st["events"]))
        status_fh.close()
        log("收工：rows=%d events=%d -> %s" % (
            session.rows_written, st["events"], csv_path))


if __name__ == "__main__":
    main()
