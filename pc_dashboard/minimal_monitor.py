"""M2 最小验证监控：连 RX 串口，解析 CIS 二进制帧，打印 EVENT 行。

用法（计划 §10 常用命令）：
    python minimal_monitor.py --port COM3 --baud 2000000
可选：
    --duration 60        运行 60s 后正常退出（缺省一直跑，Ctrl+C 退出）
    --features           同时打印 0x11 特征流行
    --heartbeat          同时打印 0x01 心跳行
    --log                同时打印 0x20 日志回显与未知类型
    --stats-interval 5   stderr 周期统计行秒数（0 关闭）

EVENT 行打到 stdout（供集成验证 grep "EVENT"）；状态/统计打到 stderr，
两者分离。退出码：0=正常结束（含 Ctrl+C），2=串口打开失败。
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import protocol
from serial_source import SerialSource


def format_event(ev: protocol.Event, seq: int | None = None) -> str:
    state = protocol.STATE_NAMES.get(ev.state, str(ev.state))
    head = "EVENT " if seq is None else "EVENT #%d " % seq
    return (head + "mac=%s name_id=%d state=%s score=%d rssi=%d ts_ms=%d"
            % (protocol.format_mac(ev.mac), ev.name_id, state, ev.score, ev.rssi, ev.ts_ms))


def format_features(f: protocol.Features) -> str:
    return ("FEATURES mac=%s ts_ms=%d mean=%d var=%d p2p=%d rssi=%d"
            % (protocol.format_mac(f.mac), f.ts_ms, f.mean, f.var, f.p2p, f.rssi))


def format_stats(stats: dict) -> str:
    keys = ("frames_ok", "bad_crc", "resyncs", "bytes_dropped", "bytes_read",
            "ring_size", "ring_high_water", "ring_dropped_bytes",
            "queue_depth", "queue_dropped", "serial_errors", "reconnects")
    parts = ["%s=%s" % (k, stats.get(k, "-")) for k in keys]
    if "device_dropped_frames" in stats:
        parts.append("device_dropped=%s" % stats["device_dropped_frames"])
    return "STATS " + " ".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CIS 最小事件监控（M2 验证）")
    ap.add_argument("--port", default="COM3", help="串口号（默认 COM3，以 MAC 为准见计划 v1.1）")
    ap.add_argument("--baud", type=int, default=2_000_000, help="波特率（默认 2000000）")
    ap.add_argument("--duration", type=float, default=None,
                    help="运行秒数后正常退出；缺省直到 Ctrl+C")
    ap.add_argument("--features", action="store_true", help="打印 0x11 特征流行")
    ap.add_argument("--heartbeat", action="store_true", help="打印 0x01 心跳行")
    ap.add_argument("--log", action="store_true", help="打印 0x20 日志回显/未知类型帧")
    ap.add_argument("--stats-interval", type=float, default=5.0,
                    help="stderr 统计行间隔秒数（0=关闭，默认 5）")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    src = SerialSource(port=args.port, baud=args.baud)
    try:
        src.start()
    except Exception as exc:
        print("ERROR: cannot open %s@%d: %s" % (args.port, args.baud, exc), file=sys.stderr)
        return 2

    print("MONITOR start port=%s baud=%d duration=%s" % (args.port, args.baud, args.duration),
          file=sys.stderr)
    events = 0
    t0 = time.monotonic()
    last_stats = t0
    try:
        while True:
            now = time.monotonic()
            if args.duration is not None and now - t0 >= args.duration:
                break
            wait = 0.5
            if args.duration is not None:
                wait = max(0.0, min(wait, args.duration - (now - t0)))
            try:
                frame = src.frame_queue.get(timeout=wait if wait > 0 else 0.01)
            except queue.Empty:
                frame = None
            if frame is not None:
                if frame.type == protocol.TYPE_EVENT:
                    try:
                        ev = protocol.decode_payload(frame)
                    except ValueError as exc:
                        print("ERROR: bad EVENT payload: %s" % exc, file=sys.stderr)
                    else:
                        events += 1
                        print(format_event(ev, events))
                elif frame.type == protocol.TYPE_FEATURES and args.features:
                    print(format_features(protocol.decode_payload(frame)))
                elif frame.type == protocol.TYPE_HEARTBEAT and args.heartbeat:
                    hb = protocol.decode_payload(frame)
                    print("HEARTBEAT uptime_ms=%d peers=%d free_heap=%d dropped=%d"
                          % (hb.uptime_ms, hb.peer_count, hb.free_heap, hb.dropped_frames))
                elif frame.type == protocol.TYPE_CALIB:
                    c = protocol.decode_payload(frame)
                    print("CALIB mac=%s train_s=%d threshold=%d noise_floor=%d"
                          % (protocol.format_mac(c.mac), c.train_duration,
                             c.threshold, c.noise_floor))
                elif frame.type == protocol.TYPE_LOG and args.log:
                    print("LOG: %s" % protocol.decode_payload(frame).text)
                elif args.log:
                    print("FRAME type=0x%02X ver=0x%02X len=%d"
                          % (frame.type, frame.ver, len(frame.payload)))
            now = time.monotonic()
            if args.stats_interval > 0 and now - last_stats >= args.stats_interval:
                print(format_stats(src.stats()), file=sys.stderr)
                last_stats = now
    except KeyboardInterrupt:
        print("MONITOR interrupted", file=sys.stderr)
    finally:
        src.stop()
        print(format_stats(src.stats()), file=sys.stderr)
        print("MONITOR done events=%d elapsed=%.1fs" % (events, time.monotonic() - t0),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
