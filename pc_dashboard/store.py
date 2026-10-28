"""CIS 数据存储：CSV 会话文件 + 内存回放缓冲（IMPLEMENTATION_PLAN.md §6 store.py）。

CSV 会话：data/YYYYMMDD_HHMM/cis_log.csv（列序固定，见 COLUMNS）：
    ts_iso,observer,mac,name,type,state,score,mean,var,p2p,rssi
并集 schema：各帧类型只填自己有的列，其余留空——
    HEARTBEAT 只有 ts_iso/type（存活证据）；
    EVENT   state/score/rssi；
    FEATURES mean/var/p2p/rssi；
    CALIB   附加 text（阈值/噪声底摘要）；
    LOG     text（ASCII 回显）。
observer 列：链路 observer 节点名（NODE_NAMES：0=TX1 1=TX2 2=TX3 3=S3）。
0x12/0x13/0x14 多节点帧填对应节点名并映射为 FEATURES/EVENT/CALIB；
0x11/0x10/0x30 遗留帧隐含 observer=上报 RX 自身，统一记 "S3"。
旧 10 列表头（无 observer，COLUMNS_LEGACY）由 header_matches 兼容识别、
replay.py 回填 "S3"。
判据"CSV 列齐全"= 表头与 COLUMNS 完全一致（Store.header_matches）。

Record：帧解析后的归一化记录（UI/存储/回放三方公共货币）。
frame_to_record() 把 protocol.Frame → Record（解析失败返回 None，不抛出）。
链路键 link_key(observer, mac) = "observer>mac"，是 UI 卡片/曲线/统计的唯一键。

回放缓冲：内存 deque 保留最近 N 条 Record（默认 3 万条 ≈ 两 peer 特征流
25 分钟），供 UI 即时回放；跨文件回放走 replay.py。

线程模型：record()/start_session()/stop_session() 加锁，可从任意线程调用；
文件句柄常开、每次写入后不强制 flush（stop_session 与每 200 行自动 flush）。
"""

from __future__ import annotations

import csv
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import protocol

# 表头即判据：M3 "CSV 列齐全" 的唯一对照（v2：mesh 12 链路，插入 observer 列）
COLUMNS = ["ts_iso", "observer", "mac", "name", "type", "state", "score",
           "mean", "var", "p2p", "rssi"]
# v1 兼容：升级前（单 RX）的 10 列表头，header_matches/replay 识别旧会话文件
COLUMNS_LEGACY = ["ts_iso", "mac", "name", "type", "state", "score",
                  "mean", "var", "p2p", "rssi"]

DEFAULT_DATA_ROOT = Path(__file__).resolve().parent.parent / "data"

_STATE_TO_NAME = {v: k for k, v in protocol.STATE_NAMES.items()}

# 节点身份：observer_id ≡ 该节点自身 CSI 源 MAC[5]（与 MAC_DEVICE_MAP 对齐）
NODE_NAMES = {0: "TX1", 1: "TX2", 2: "TX3", 3: "S3"}


def node_name(observer_id: int) -> str:
    """observer_id → 节点名；未知 id 保留十六进制原值便于排查。"""
    return NODE_NAMES.get(observer_id, "0x%02X" % observer_id)


def link_key(observer: str, mac: str) -> str:
    """链路键 "observer>mac"：UI 卡片/曲线/新鲜度/标定进度的唯一键。"""
    return "%s>%s" % (observer, mac)


def default_zone_name(mac: str) -> str:
    """无映射时的区域缺省名（取 MAC 末两字节，如 区域-ae10）。"""
    return "区域-" + mac.replace(":", "")[-4:]


@dataclass
class Record:
    """归一化记录。kind ∈ {HEARTBEAT, EVENT, FEATURES, LOG, CALIB}——
    0x12/0x13/0x14 多节点帧映射为 FEATURES/EVENT/CALIB，observer 填节点名。"""
    ts: float                    # epoch 秒（接收时刻 / 回放重定基时刻）
    mac: str                     # 格式化 MAC；HEARTBEAT/LOG 为 ""
    kind: str
    name: str = ""               # 区域名（查不到留空，UI 负责起名）
    observer: str = ""           # 链路 observer 节点名（HEARTBEAT/LOG 为 ""）
    state: int | None = None     # EVENT
    score: int | None = None     # EVENT
    mean: int | None = None      # FEATURES
    var: int | None = None       # FEATURES
    p2p: int | None = None       # FEATURES
    rssi: int | None = None      # EVENT / FEATURES
    raw: object = None           # 原始解码对象（protocol.Event/Features/...）
    text: str = ""               # LOG 文本 / CALIB 摘要

    @property
    def ts_iso(self) -> str:
        return datetime.fromtimestamp(self.ts).isoformat(timespec="milliseconds")

    def state_name(self) -> str:
        if self.state is None:
            return ""
        return protocol.STATE_NAMES.get(self.state, str(self.state))

    def to_row(self) -> list:
        """按 COLUMNS 顺序展开成一行（None → 空串）。"""
        return [
            self.ts_iso, self.observer, self.mac, self.name, self.kind,
            self.state_name() if self.kind == "EVENT" else "",
            "" if self.score is None else self.score,
            "" if self.mean is None else self.mean,
            "" if self.var is None else self.var,
            "" if self.p2p is None else self.p2p,
            "" if self.rssi is None else self.rssi,
        ]


def frame_to_record(frame: "protocol.Frame", ts: float | None = None,
                    name_lookup: Callable[[str, int], str] | None = None):
    """protocol.Frame → Record。payload 解码失败返回 None（记入调用方统计）。

    name_lookup(mac_str, name_id) → 区域名；未提供或返回空则 Record.name 留空。
    0x12/0x13/0x14 多节点帧：observer=node_name(observer_id)，映射为
    FEATURES/EVENT/CALIB（与 0x11/0x10/0x30 同 kind，UI/CSV 无需分支）。
    0x11/0x10/0x30 遗留帧：隐含 observer=上报 RX 自身 → NODE_NAMES[3]（"S3"）。
    """
    ts = time.time() if ts is None else ts
    try:
        obj = protocol.decode_payload(frame)
    except Exception:
        return None
    tname = frame.type_name

    if frame.type == protocol.TYPE_HEARTBEAT:
        return Record(ts=ts, mac="", kind=tname, raw=obj)
    if frame.type == protocol.TYPE_EVENT:
        mac = protocol.format_mac(obj.mac)
        name = name_lookup(mac, obj.name_id) if name_lookup else ""
        return Record(ts=ts, mac=mac, kind=tname, name=name,
                      observer=NODE_NAMES[3],
                      state=obj.state, score=obj.score, rssi=obj.rssi, raw=obj)
    if frame.type == protocol.TYPE_FEATURES:
        mac = protocol.format_mac(obj.mac)
        name = name_lookup(mac, 0) if name_lookup else ""
        return Record(ts=ts, mac=mac, kind=tname, name=name,
                      observer=NODE_NAMES[3],
                      mean=obj.mean, var=obj.var, p2p=obj.p2p,
                      rssi=obj.rssi, raw=obj)
    if frame.type == protocol.TYPE_FEATURE_MULTI:
        mac = protocol.format_mac(obj.mac)
        name = name_lookup(mac, 0) if name_lookup else ""
        return Record(ts=ts, mac=mac, kind="FEATURES", name=name,
                      observer=node_name(obj.observer_id),
                      mean=obj.mean, var=obj.var, p2p=obj.p2p,
                      rssi=obj.rssi, raw=obj)
    if frame.type == protocol.TYPE_EVENT_MULTI:
        mac = protocol.format_mac(obj.mac)
        name = name_lookup(mac, obj.name_id) if name_lookup else ""
        return Record(ts=ts, mac=mac, kind="EVENT", name=name,
                      observer=node_name(obj.observer_id),
                      state=obj.state, score=obj.score, rssi=obj.rssi, raw=obj)
    if frame.type == protocol.TYPE_CALIB:
        mac = protocol.format_mac(obj.mac)
        name = name_lookup(mac, 0) if name_lookup else ""
        return Record(ts=ts, mac=mac, kind=tname, name=name, raw=obj,
                      observer=NODE_NAMES[3],
                      text="train %ds 阈值=%d 噪声底=%d"
                           % (obj.train_duration, obj.threshold, obj.noise_floor))
    if frame.type == protocol.TYPE_CALIB_MULTI:
        mac = protocol.format_mac(obj.mac)
        name = name_lookup(mac, 0) if name_lookup else ""
        return Record(ts=ts, mac=mac, kind="CALIB", name=name, raw=obj,
                      observer=node_name(obj.observer_id),
                      text="train %ds 阈值=%d 噪声底=%d"
                           % (obj.train_s, obj.threshold, obj.noise_floor))
    if frame.type == protocol.TYPE_LOG:
        return Record(ts=ts, mac="", kind=tname, raw=obj, text=obj.text)
    return Record(ts=ts, mac="", kind=tname, raw=obj,
                  text=repr(obj)[:120])


class Store:
    """CSV 会话写入 + 回放缓冲。用法：
        st = Store()                       # data 根默认 <工程>/data
        st.start_session()                 # 建 data/YYYYMMDD_HHMM/cis_log.csv
        st.record(rec)                     # 写一行 + 进回放缓冲
        st.stop_session()
    """

    def __init__(self, data_root: str | Path | None = None,
                 replay_buffer_size: int = 30000, flush_every: int = 200):
        self.data_root = Path(data_root) if data_root else DEFAULT_DATA_ROOT
        self.session_dir: Path | None = None
        self.csv_path: Path | None = None
        self.rows_written = 0
        self.replay_buffer: deque = deque(maxlen=replay_buffer_size)
        self.write_errors = 0
        self._flush_every = flush_every
        self._fh = None
        self._writer = None
        self._lock = threading.Lock()

    # ---- 会话生命周期 ------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._fh is not None

    def start_session(self, session_name: str | None = None) -> Path:
        """创建 data/YYYYMMDD_HHMM[-名字]/cis_log.csv 并写表头。
        同分钟内重复开会话自动加后缀 _2/_3…，不覆盖历史。"""
        with self._lock:
            if self._fh is not None:
                return self.csv_path
            base = datetime.now().strftime("%Y%m%d_%H%M")
            if session_name:
                base += "-" + "".join(c for c in session_name if c.isalnum() or c in "-_")[:32]
            d = self.data_root / base
            suffix = 1
            while d.exists() and (d / "cis_log.csv").exists():
                suffix += 1
                d = self.data_root / ("%s_%d" % (base, suffix))
            d.mkdir(parents=True, exist_ok=True)
            path = d / "cis_log.csv"
            fh = open(path, "w", newline="", encoding="utf-8")
            w = csv.writer(fh)
            w.writerow(COLUMNS)
            fh.flush()
            self.session_dir, self.csv_path = d, path
            self._fh, self._writer = fh, w
            self.rows_written = 0
            return path

    def stop_session(self) -> None:
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                self.write_errors += 1
            self._fh = self._writer = None

    # ---- 写入 --------------------------------------------------------------

    def record(self, rec: Record) -> bool:
        """写一行并推进回放缓冲。未在记录时只进缓冲。返回是否落盘。"""
        if rec is None:
            return False
        self.replay_buffer.append(rec)
        with self._lock:
            if self._writer is None:
                return False
            try:
                self._writer.writerow(rec.to_row())
                self.rows_written += 1
                if self.rows_written % self._flush_every == 0:
                    self._fh.flush()
                return True
            except Exception:
                self.write_errors += 1
                return False

    # ---- 校验/诊断 ---------------------------------------------------------

    def header_matches(self) -> bool:
        """已写 CSV 的首行是否与 COLUMNS 完全一致（M3 判据）。
        兼容旧 10 列表头（COLUMNS_LEGACY，升级前的会话文件）。"""
        if self.csv_path is None or not self.csv_path.exists():
            return False
        try:
            with open(self.csv_path, "r", encoding="utf-8") as fh:
                return next(csv.reader(fh)) in (COLUMNS, COLUMNS_LEGACY)
        except Exception:
            return False

    def stats(self) -> dict:
        return {"rows_written": self.rows_written,
                "recording": self.is_recording,
                "replay_buffer": len(self.replay_buffer),
                "write_errors": self.write_errors,
                "csv_path": str(self.csv_path) if self.csv_path else ""}
