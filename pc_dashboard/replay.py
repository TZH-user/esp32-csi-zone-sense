"""CSV 按时间戳倍速回放（IMPLEMENTATION_PLAN.md §6 replay.py）。

CsvReplayer 读 store.py 写出的 cis_log.csv，把每行还原成 Record，按原始
ts_iso 间隔以 speed 倍速逐条回调 on_record：

    rp = CsvReplayer(path, on_record=ui_put, speed=4.0)
    rp.start() ... rp.stop()

    speed>1 快放、<1 慢放；loop=True 循环放。
    时间重定基：首行 ts → start() 时刻的墙钟，后续 rec.ts = base +
    (t_i - t_0)/speed —— 回放在 UI 里看起来与实时流一致（曲线 x 轴跟随）。

Record.ts 被重定基后直接进 Dashboard 的分发管线（进卡片/曲线/事件表，
重新记录则形成新 CSV 会话）。缺列/坏时间戳/未知 type 行跳过并计数。

线程模型：单回放线程 sleep 驱动；on_record 在回放线程调用——UI 端应传入
"塞队列"回调而不是直接动控件。stop() 后线程 1s 内退出。
"""

from __future__ import annotations

import csv
import dataclasses
import threading
import time
from datetime import datetime
from pathlib import Path

from store import COLUMNS, Record, default_zone_name

_STATE_FROM_NAME = {k: v for k, v in
                    {"STILL": 0, "MOTION": 1, "PRESENCE": 2}.items()}


def parse_ts_iso(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


class CsvReplayer:
    """csv → Record 流。接口与数据源风格一致：start()/stop()/stats()。

    注意：与 SerialSource 不同，本类不产生 protocol.Frame（CSV 已丢失
    二进制 payload 全部字段），直接吐 Record——Dashboard 两条入队路径
    （frame_queue→frame_to_record / on_record 直入）最终汇合同一分发点。
    """

    def __init__(self, path: str | Path, on_record, speed: float = 1.0,
                 loop: bool = False, max_gap: float | None = None):
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.path = Path(path)
        self.on_record = on_record
        self.speed = float(speed)
        self.loop = loop
        self.max_gap = max_gap          # None=忠实回放；设值则截断超长空窗
        self.rows_total = 0
        self.rows_sent = 0
        self.rows_skipped = 0
        self.running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 载入 --------------------------------------------------------------

    def _load(self) -> list[Record]:
        """读 CSV 还原 Record（ts 先保留原始 epoch，start 时重定基）。"""
        with open(self.path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("empty csv: %s" % self.path)
            if header != COLUMNS:
                raise ValueError("csv header mismatch: %s != %s"
                                 % (header, COLUMNS))
            out = []
            for row in reader:
                self.rows_total += 1
                try:
                    rec = self._row_to_record(row)
                except Exception:
                    self.rows_skipped += 1
                    continue
                if rec is not None:
                    out.append(rec)
                else:
                    self.rows_skipped += 1
        if not out:
            raise ValueError("no replayable rows in %s" % self.path)
        out.sort(key=lambda r: r.ts)
        return out

    def _row_to_record(self, row: list) -> Record | None:
        if len(row) < len(COLUMNS):
            raise ValueError("short row")
        (ts_iso, mac, name, kind, state, score,
         mean, var, p2p, rssi) = row[:len(COLUMNS)]
        ts = parse_ts_iso(ts_iso)
        rec = Record(ts=ts, mac=mac, kind=kind,
                     name=name or (default_zone_name(mac) if mac else ""))
        if kind == "EVENT":
            rec.state = _STATE_FROM_NAME.get(state)
            rec.score = int(score) if score else None
            rec.rssi = int(rssi) if rssi else None
        elif kind == "FEATURES":
            rec.mean = int(mean) if mean else None
            rec.var = int(var) if var else None
            rec.p2p = int(p2p) if p2p else None
            rec.rssi = int(rssi) if rssi else None
        elif kind == "LOG":
            rec.text = ""           # CSV 不存 LOG 正文（text 列不在 schema）
        elif kind in ("HEARTBEAT", "CALIB"):
            pass                    # 摘要字段未入 CSV，仅保留时间线
        else:
            return None             # 未知 type：跳过
        return rec

    # ---- 回放线程 ----------------------------------------------------------

    def start(self):
        if self._thread is not None:
            raise RuntimeError("CsvReplayer already started")
        records = self._load()
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._run, args=(records,),
                                        name="cis-replay", daemon=True)
        self._thread.start()

    def _run(self, records: list[Record]):
        t0 = records[0].ts
        try:
            while not self._stop.is_set():
                base_wall = time.time()
                for rec in records:
                    if self._stop.is_set():
                        return
                    target = base_wall + (rec.ts - t0) / self.speed
                    if self.max_gap is not None:
                        target = min(target, time.time() + self.max_gap)
                    delay = target - time.time()
                    if delay > 0 and self._stop.wait(delay):
                        return
                    out = dataclasses.replace(rec)
                    out.ts = time.time()          # 重定基为墙钟
                    try:
                        self.on_record(out)
                    except Exception:
                        pass                      # UI 回调异常不终止回放
                    self.rows_sent += 1
                if not self.loop:
                    return
        finally:
            self.running = False

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
            self._thread = None
        self.running = False

    def stats(self) -> dict:
        return {"replay_path": str(self.path), "speed": self.speed,
                "rows_total": self.rows_total, "rows_sent": self.rows_sent,
                "rows_skipped": self.rows_skipped, "running": self.running}
