"""CIS 上报协议帧编解码 —— 纯标准库（Python 3，无第三方依赖）。

对照源：docs/IMPLEMENTATION_PLAN.md §5。
固件侧落定 main/cis_protocol.h 后，以其为实现对照；两端必须一致：
    - CRC 变体：CRC-16/CCITT-FALSE（poly=0x1021、init=0xFFFF、不反射、无异或输出），
      标准校验值 crc16_ccitt(b"123456789") == 0x29B1；
    - 多字节字段一律小端（len/crc 显式 LE，payload 字段按 ESP32 内存序同为 LE）。

帧格式：
    0xAA 0x55 | ver(1B=0x01) | type(1B) | len(2B,LE) | payload(len B) | crc16(2B,LE)
CRC 覆盖 ver..payload（即 magic 之后、crc 之前的全部字节），不含 magic 与 CRC 本身。

帧类型（§5）：
    0x01 心跳     uptime_ms u32, peer_count u8, free_heap u32, dropped_frames u16   (11B)
    0x10 事件     mac 6B, name_id u8, state u8(0静止/1运动/2存在), score u16,
                  rssi i8, ts_ms u32                                              (15B)
    0x11 特征流   mac 6B, ts_ms u32, mean u16, var u16, p2p u16, rssi i8           (17B)
    0x12 特征·多节点 observer_id u8, source_mac 6B, ts_ms u32, mean u16, var u16,
                  p2p u16, rssi i8                                                (18B)
    0x13 事件·多节点 observer_id u8, source_mac 6B, name_id u8, state u8, score u16,
                  rssi i8, ts_ms u32                                              (16B)
    0x14 标定·多节点 observer_id u8, source_mac 6B, train_s u16, threshold u16,
                  noise_floor u16                                                 (13B)
    0x20 日志回显 ASCII 原始字节，变长
    0x30 标定结果 mac 6B, train_duration u16, threshold u16, noise_floor u16       (12B)

多节点帧（0x12/0x13/0x14，mesh 升级新增，与固件 main/cis_protocol.h 互为对照源，
固件侧随 S3 mesh 升级同步增补）：
    - observer_id = 发送方节点自身 CSI 源 MAC[5]（零配置节点身份）：
      0x00=TX1  0x01=TX2  0x02=TX3  0x03=S3 RX；
      source_mac = 被观测源（CSI 发送方）MAC。链路 = (observer, source)。
    - 0x11/0x10/0x30 隐含 observer=上报 RX 自身（单 RX 遗留）；0x12/0x13/0x14
      显式携带 observer，其余字段单位刻度与 0x11/0x10/0x30 完全一致
      （centi-amp/var）。升级后 S3 只发多节点帧，PC 保留旧类型解析以兼容
      旧 CSV/回放。
    - ts_ms 为各 observer 本地 uptime（esp_log_timestamp），跨 observer 不可比，
      仅限链路内单调比较与到达时间排序。

流式解析（StreamParser）面向"控制台日志字节与二进制帧交织"的现实串口流：
    - 全程搜索 0xAA 0x55 魔数，噪声字节计入 bytes_dropped；
    - 候选帧 CRC 不匹配 → 只前移 1 字节重扫（失步重同步），不吞掉可能嵌在
      候选帧内部的真帧；
    - len 超过 MAX_PAYLOAD 的候选头视为噪声前移 1 字节；
    - ver 仅透传不做硬校验（CRC 是唯一准入门槛，向前兼容版本升级）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 帧常量
# ---------------------------------------------------------------------------

MAGIC = b"\xaa\x55"
VERSION = 0x01
HEADER_LEN = 6        # magic(2) + ver(1) + type(1) + len(2)
CRC_LEN = 2
MAX_PAYLOAD = 1024    # 坏 len 防护；协议最大定长 payload 18B，日志帧上限 1KB

TYPE_HEARTBEAT = 0x01
TYPE_EVENT = 0x10
TYPE_FEATURES = 0x11
TYPE_FEATURE_MULTI = 0x12
TYPE_EVENT_MULTI = 0x13
TYPE_CALIB_MULTI = 0x14
TYPE_LOG = 0x20
TYPE_CALIB = 0x30

TYPE_NAMES = {
    TYPE_HEARTBEAT: "HEARTBEAT",
    TYPE_EVENT: "EVENT",
    TYPE_FEATURES: "FEATURES",
    TYPE_FEATURE_MULTI: "FEATURE_MULTI",
    TYPE_EVENT_MULTI: "EVENT_MULTI",
    TYPE_CALIB_MULTI: "CALIB_MULTI",
    TYPE_LOG: "LOG",
    TYPE_CALIB: "CALIB",
}

STATE_STILL = 0
STATE_MOTION = 1
STATE_PRESENCE = 2
STATE_NAMES = {STATE_STILL: "STILL", STATE_MOTION: "MOTION", STATE_PRESENCE: "PRESENCE"}


# ---------------------------------------------------------------------------
# CRC-16/CCITT-FALSE（表驱动）
# ---------------------------------------------------------------------------

def _make_table():
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _make_table()


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE。crc 参数支持续算；init 传 0 即 XMODEM 变体。"""
    table = _CRC_TABLE
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ table[((crc >> 8) ^ b) & 0xFF]
    return crc


def crc16_ccitt_bitwise(data: bytes, crc: int = 0xFFFF) -> int:
    """同上但逐位实现，仅用于交叉验证（selftest），生产代码用表驱动版。"""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


# ---------------------------------------------------------------------------
# payload 数据类（字段序 = 线上字节序，pack/unpack 用同一 Struct）
# ---------------------------------------------------------------------------

@dataclass
class Heartbeat:
    uptime_ms: int
    peer_count: int
    free_heap: int
    dropped_frames: int          # 固件侧丢帧计数（环/队列溢出等）
    STRUCT = struct.Struct("<IBIH")   # 11B


@dataclass
class Event:
    mac: bytes                   # 6B 源 MAC（TX1 恒为 1a:00:00:00:00:00，见计划 v1.2 §2）
    name_id: int                 # 区域名编号（peer 表分配）
    state: int                   # 0=STILL 1=MOTION 2=PRESENCE
    score: int
    rssi: int                    # 有符号 dBm
    ts_ms: int
    STRUCT = struct.Struct("<6sBBHbI")   # 15B


@dataclass
class Features:
    mac: bytes
    ts_ms: int
    mean: int
    var: int
    p2p: int
    rssi: int
    STRUCT = struct.Struct("<6sIHHHb")   # 17B


@dataclass
class FeaturesMulti:
    """0x12 特征·多节点：observer_id=发送节点自身 MAC[5]，mac=被观测源 MAC。"""
    observer_id: int
    mac: bytes
    ts_ms: int
    mean: int
    var: int
    p2p: int
    rssi: int
    STRUCT = struct.Struct("<B6sIHHHb")   # 18B


@dataclass
class EventMulti:
    """0x13 事件·多节点：字段含义与 0x10 一致，增加 observer_id 前缀。"""
    observer_id: int
    mac: bytes
    name_id: int
    state: int
    score: int
    rssi: int
    ts_ms: int
    STRUCT = struct.Struct("<B6sBBHbI")   # 16B


@dataclass
class CalibMulti:
    """0x14 标定·多节点：字段含义与 0x30 一致，增加 observer_id 前缀。"""
    observer_id: int
    mac: bytes
    train_s: int
    threshold: int
    noise_floor: int
    STRUCT = struct.Struct("<B6sHHH")   # 13B


@dataclass
class LogEcho:
    raw: bytes

    @property
    def text(self) -> str:
        return self.raw.decode("ascii", errors="replace")


@dataclass
class CalibResult:
    mac: bytes
    train_duration: int
    threshold: int
    noise_floor: int
    STRUCT = struct.Struct("<6sHHH")   # 12B


@dataclass
class UnknownPayload:
    """未知类型的 payload 原样保留（前向兼容）。"""
    type: int
    raw: bytes


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------

def encode_frame(ftype: int, payload: bytes = b"", ver: int = VERSION) -> bytes:
    """按 §5 帧格式编码。payload 超过 MAX_PAYLOAD 抛 ValueError。"""
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too large: %d > %d" % (len(payload), MAX_PAYLOAD))
    body = struct.pack("<BBH", ver, ftype, len(payload)) + payload
    return MAGIC + body + struct.pack("<H", crc16_ccitt(body))


def build_heartbeat(f: Heartbeat, ver: int = VERSION) -> bytes:
    return encode_frame(TYPE_HEARTBEAT, f.STRUCT.pack(
        f.uptime_ms, f.peer_count, f.free_heap, f.dropped_frames), ver)


def build_event(f: Event, ver: int = VERSION) -> bytes:
    return encode_frame(TYPE_EVENT, f.STRUCT.pack(
        f.mac, f.name_id, f.state, f.score, f.rssi, f.ts_ms), ver)


def build_features(f: Features, ver: int = VERSION) -> bytes:
    return encode_frame(TYPE_FEATURES, f.STRUCT.pack(
        f.mac, f.ts_ms, f.mean, f.var, f.p2p, f.rssi), ver)


def build_features_multi(f: FeaturesMulti, ver: int = VERSION) -> bytes:
    return encode_frame(TYPE_FEATURE_MULTI, f.STRUCT.pack(
        f.observer_id, f.mac, f.ts_ms, f.mean, f.var, f.p2p, f.rssi), ver)


def build_event_multi(f: EventMulti, ver: int = VERSION) -> bytes:
    return encode_frame(TYPE_EVENT_MULTI, f.STRUCT.pack(
        f.observer_id, f.mac, f.name_id, f.state, f.score, f.rssi, f.ts_ms), ver)


def build_calib_multi(f: CalibMulti, ver: int = VERSION) -> bytes:
    return encode_frame(TYPE_CALIB_MULTI, f.STRUCT.pack(
        f.observer_id, f.mac, f.train_s, f.threshold, f.noise_floor), ver)


def build_log(text: str, ver: int = VERSION) -> bytes:
    return encode_frame(TYPE_LOG, text.encode("ascii", errors="replace"), ver)


def build_calib(f: CalibResult, ver: int = VERSION) -> bytes:
    return encode_frame(TYPE_CALIB, f.STRUCT.pack(
        f.mac, f.train_duration, f.threshold, f.noise_floor), ver)


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------

_EXPECTED_LEN = {
    TYPE_HEARTBEAT: Heartbeat.STRUCT.size,
    TYPE_EVENT: Event.STRUCT.size,
    TYPE_FEATURES: Features.STRUCT.size,
    TYPE_FEATURE_MULTI: FeaturesMulti.STRUCT.size,
    TYPE_EVENT_MULTI: EventMulti.STRUCT.size,
    TYPE_CALIB_MULTI: CalibMulti.STRUCT.size,
    TYPE_CALIB: CalibResult.STRUCT.size,
}


def decode_payload(frame: "Frame"):
    """按 frame.type 解出对应数据类；未知类型返回 UnknownPayload。

    payload 长度不符抛 ValueError（CRC 已过但长度错，属协议两端不一致）。
    显式校验长度而非依赖 struct.error：后者不是 ValueError 子类，直接 unpack
    会让只捕获 ValueError 的调用方（minimal_monitor EVENT 分支）崩溃。
    """
    t, p = frame.type, frame.payload
    expect = _EXPECTED_LEN.get(t)
    if expect is not None and len(p) != expect:
        raise ValueError("payload length %d != expected %d for type 0x%02X"
                         % (len(p), expect, t))
    if t == TYPE_HEARTBEAT:
        return Heartbeat(*Heartbeat.STRUCT.unpack(p))
    if t == TYPE_EVENT:
        return Event(*Event.STRUCT.unpack(p))
    if t == TYPE_FEATURES:
        return Features(*Features.STRUCT.unpack(p))
    if t == TYPE_FEATURE_MULTI:
        return FeaturesMulti(*FeaturesMulti.STRUCT.unpack(p))
    if t == TYPE_EVENT_MULTI:
        return EventMulti(*EventMulti.STRUCT.unpack(p))
    if t == TYPE_CALIB_MULTI:
        return CalibMulti(*CalibMulti.STRUCT.unpack(p))
    if t == TYPE_LOG:
        return LogEcho(p)
    if t == TYPE_CALIB:
        return CalibResult(*CalibResult.STRUCT.unpack(p))
    return UnknownPayload(t, p)


@dataclass
class Frame:
    ver: int
    type: int
    payload: bytes

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, "0x%02X" % self.type)


def format_mac(mac: bytes) -> str:
    return ":".join("%02x" % b for b in mac)


# ---------------------------------------------------------------------------
# 流式解析（容忍控制台日志交织 + 失步重同步）
# ---------------------------------------------------------------------------

class StreamParser:
    """喂字节、吐 Frame。可跨 feed() 调用保持半帧状态。

    统计字段（供上位机界面展示）：
        frames_ok      成功CRC通过的帧数
        bad_crc        CRC不匹配次数
        resyncs        失步/假帧头重同步次数
        bytes_dropped  未构成有效帧而被丢弃的字节数（含控制台文本）
    """

    _HDR = struct.Struct("<BBH")   # ver, type, len（magic 之后）

    def __init__(self, max_payload: int = MAX_PAYLOAD):
        self._buf = bytearray()
        self._max_payload = max_payload
        self.frames_ok = 0
        self.bad_crc = 0
        self.resyncs = 0
        self.bytes_dropped = 0

    def feed(self, data: bytes):
        """输入任意长度字节，返回本次解出的 Frame 列表（可能为空）。"""
        self._buf += data
        frames = []
        while True:
            frame, progressed = self._try_parse()
            if frame is not None:
                frames.append(frame)
            if not progressed:
                break                  # 真·需要更多数据才停；重同步路径会继续
        return frames

    def _try_parse(self):
        """返回 (Frame|None, progressed)。progressed=本次调用消耗了字节或出了帧。"""
        buf = self._buf
        # 1) 找魔数
        i = buf.find(MAGIC)
        if i < 0:
            # 无完整魔数：除末 1 字节（可能是被截断的 0xAA）外全丢
            drop = len(buf) - 1
            if drop > 0:
                self.bytes_dropped += drop
                del buf[:drop]
                return None, True
            return None, False
        if i > 0:
            self.bytes_dropped += i
            del buf[:i]
            return None, True
        # 2) 读头（magic 已就位，还需 4B）
        if len(buf) < HEADER_LEN:
            return None, False
        ver, ftype, plen = self._HDR.unpack_from(buf, 2)
        if plen > self._max_payload:
            # 假帧头：前移 1 字节重扫
            self.resyncs += 1
            self.bytes_dropped += 1
            del buf[:1]
            return None, True
        # 3) 收齐整帧
        total = HEADER_LEN + plen + CRC_LEN
        if len(buf) < total:
            return None, False
        body = bytes(buf[2:HEADER_LEN + plen])
        (crc_got,) = struct.unpack_from("<H", buf, HEADER_LEN + plen)
        if crc16_ccitt(body) != crc_got:
            # CRC 不符：只前移 1 字节，保留嵌在其后的真帧
            self.bad_crc += 1
            self.resyncs += 1
            self.bytes_dropped += 1
            del buf[:1]
            return None, True
        payload = bytes(buf[HEADER_LEN:HEADER_LEN + plen])
        del buf[:total]
        self.frames_ok += 1
        return Frame(ver=ver, type=ftype, payload=payload), True

    @property
    def pending(self) -> int:
        """当前缓冲中未成帧的字节数（诊断用）。"""
        return len(self._buf)

    def stats(self) -> dict:
        return {
            "frames_ok": self.frames_ok,
            "bad_crc": self.bad_crc,
            "resyncs": self.resyncs,
            "bytes_dropped": self.bytes_dropped,
            "pending": self.pending,
        }
