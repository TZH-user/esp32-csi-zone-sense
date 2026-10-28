"""protocol.py 确定性自测 —— 纯标准库，作工作流门控。

覆盖：CRC 校验值 / 全类型编解码往返（含极值）/ 坏帧拒绝 / 失步重同步 /
分片喂入 / 控制台文本交织 / 截断帧。退出码 0=全绿，1=存在失败。
"""

import os
import random
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import protocol as P
from protocol import (
    MAGIC, VERSION, MAX_PAYLOAD,
    TYPE_HEARTBEAT, TYPE_EVENT, TYPE_FEATURES, TYPE_LOG, TYPE_CALIB,
    TYPE_FEATURE_MULTI, TYPE_EVENT_MULTI, TYPE_CALIB_MULTI,
    STATE_STILL, STATE_MOTION, STATE_PRESENCE, STATE_NAMES,
    StreamParser, decode_payload, encode_frame,
    build_heartbeat, build_event, build_features, build_log, build_calib,
    build_features_multi, build_event_multi, build_calib_multi,
    crc16_ccitt, crc16_ccitt_bitwise, format_mac,
    Heartbeat, Event, Features, LogEcho, CalibResult,
    FeaturesMulti, EventMulti, CalibMulti, UnknownPayload,
)

MAC1 = bytes.fromhex("1a0000000000")   # TX1 的 CSI 身份 MAC（计划 v1.2 §2）
MAC2 = bytes.fromhex("e4b06333f040")   # TX2 (C3)
HEADER_OFF_PAYLOAD = 6                 # magic(2)+ver(1)+type(1)+len(2) 之后的偏移


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------

def test_crc16_known_vectors():
    # CRC-16/CCITT-FALSE 标准校验值
    assert crc16_ccitt(b"123456789") == 0x29B1, hex(crc16_ccitt(b"123456789"))
    # 逐位实现与表驱动一致
    assert crc16_ccitt_bitwise(b"123456789") == 0x29B1
    # init=0 即 XMODEM 变体（校验值 0x31C3）
    assert crc16_ccitt(b"123456789", 0x0000) == 0x31C3
    # 空输入 = 初值
    assert crc16_ccitt(b"") == 0xFFFF
    # 续算一致性
    assert crc16_ccitt(b"6789", crc16_ccitt(b"12345")) == crc16_ccitt(b"123456789")


# ---------------------------------------------------------------------------
# 编解码往返
# ---------------------------------------------------------------------------

def test_roundtrip_heartbeat():
    for h in (
        Heartbeat(0, 0, 0, 0),
        Heartbeat(0xFFFFFFFF, 255, 0xFFFFFFFF, 0xFFFF),
        Heartbeat(3600123, 3, 201312, 7),
    ):
        frames = StreamParser().feed(build_heartbeat(h))
        assert len(frames) == 1
        assert frames[0].type == TYPE_HEARTBEAT and frames[0].ver == VERSION
        got = decode_payload(frames[0])
        assert got == h, (got, h)


def test_roundtrip_event_extremes():
    for e in (
        Event(MAC1, 1, STATE_MOTION, 87, -6, 45678),
        Event(b"\x00" * 6, 0, STATE_STILL, 0, 0, 0),
        Event(b"\xff" * 6, 255, STATE_PRESENCE, 0xFFFF, -128, 0xFFFFFFFF),
        Event(MAC2, 2, STATE_MOTION, 65535, 127, 86399999),
    ):
        frames = StreamParser().feed(build_event(e))
        assert len(frames) == 1 and frames[0].type == TYPE_EVENT
        got = decode_payload(frames[0])
        assert got == e, (got, e)


def test_roundtrip_features():
    for f in (
        Features(MAC1, 1000, 3793, 12, 64, -6),
        Features(MAC2, 0xFFFFFFFF, 0, 0, 0, -128),
        Features(MAC1, 1, 0xFFFF, 0xFFFF, 0xFFFF, 127),
    ):
        frames = StreamParser().feed(build_features(f))
        assert len(frames) == 1 and frames[0].type == TYPE_FEATURES
        assert decode_payload(frames[0]) == f


def test_roundtrip_multi_frames():
    # 0x12/0x13/0x14 多节点帧：observer_id + 与 0x11/0x10/0x30 同刻度字段
    for fm in (
        FeaturesMulti(3, MAC1, 1000, 3793, 12, 64, -6),
        FeaturesMulti(0, MAC2, 0xFFFFFFFF, 0, 0, 0, -128),
        FeaturesMulti(0xFF, b"\xff" * 6, 1, 0xFFFF, 0xFFFF, 0xFFFF, 127),
    ):
        frames = StreamParser().feed(build_features_multi(fm))
        assert len(frames) == 1 and frames[0].type == TYPE_FEATURE_MULTI
        assert frames[0].type_name == "FEATURE_MULTI"
        assert decode_payload(frames[0]) == fm
    for em in (
        EventMulti(3, MAC1, 1, STATE_MOTION, 87, -6, 45678),
        EventMulti(0, b"\x00" * 6, 0, STATE_STILL, 0, 0, 0),
        EventMulti(2, b"\xff" * 6, 255, STATE_PRESENCE, 0xFFFF, -128, 0xFFFFFFFF),
    ):
        frames = StreamParser().feed(build_event_multi(em))
        assert len(frames) == 1 and frames[0].type == TYPE_EVENT_MULTI
        assert frames[0].type_name == "EVENT_MULTI"
        assert decode_payload(frames[0]) == em
    for cm in (
        CalibMulti(3, MAC1, 15, 340, 55),
        CalibMulti(0, b"\xff" * 6, 0xFFFF, 0xFFFF, 0xFFFF),
        CalibMulti(1, MAC2, 0, 0, 0),
    ):
        frames = StreamParser().feed(build_calib_multi(cm))
        assert len(frames) == 1 and frames[0].type == TYPE_CALIB_MULTI
        assert frames[0].type_name == "CALIB_MULTI"
        assert decode_payload(frames[0]) == cm


def test_multi_expected_len_guard():
    # 三条新 _EXPECTED_LEN 精确长度（与设计 18/16/13B 一致）
    assert P._EXPECTED_LEN[TYPE_FEATURE_MULTI] == 18
    assert P._EXPECTED_LEN[TYPE_EVENT_MULTI] == 16
    assert P._EXPECTED_LEN[TYPE_CALIB_MULTI] == 13
    assert FeaturesMulti.STRUCT.size == 18
    assert EventMulti.STRUCT.size == 16
    assert CalibMulti.STRUCT.size == 13
    # CRC 已过但长度不符 → 显式 ValueError（与 0x11 同守护，不靠 struct.error）
    for t, bad in ((TYPE_FEATURE_MULTI, b"\x00" * 17),
                   (TYPE_FEATURE_MULTI, b"\x00" * 19),
                   (TYPE_EVENT_MULTI, b"\x00" * 15),
                   (TYPE_EVENT_MULTI, b"\x00" * 17),
                   (TYPE_CALIB_MULTI, b"\x00" * 12),
                   (TYPE_CALIB_MULTI, b"\x00" * 14)):
        try:
            decode_payload(P.Frame(ver=VERSION, type=t, payload=bad))
        except ValueError:
            pass
        else:
            raise AssertionError("type 0x%02X 长度 %d 未被拒绝" % (t, len(bad)))


def test_unknown_type_compat():
    # 未知类型（如未来 0x15）：CRC 通过 → UnknownPayload 原样保留（前向兼容），
    # 旧上位机只多计未知帧不崩——PC 先行的升级顺序依赖此分支
    frames = StreamParser().feed(encode_frame(0x15, b"\x01\x02\x03"))
    assert len(frames) == 1
    got = decode_payload(frames[0])
    assert isinstance(got, UnknownPayload)
    assert got.type == 0x15 and got.raw == b"\x01\x02\x03"
    assert frames[0].type_name == "0x15"


def test_roundtrip_log_and_calib():
    for text in ("", "hello console", "x" * MAX_PAYLOAD):
        frames = StreamParser().feed(build_log(text))
        assert len(frames) == 1 and frames[0].type == TYPE_LOG
        got = decode_payload(frames[0])
        assert isinstance(got, LogEcho) and got.text == text
    for c in (
        CalibResult(MAC1, 15, 340, 55),
        CalibResult(b"\xff" * 6, 0xFFFF, 0xFFFF, 0xFFFF),
        CalibResult(MAC2, 0, 0, 0),
    ):
        frames = StreamParser().feed(build_calib(c))
        assert len(frames) == 1 and frames[0].type == TYPE_CALIB
        assert decode_payload(frames[0]) == c


def test_payload_size_guard():
    try:
        encode_frame(TYPE_LOG, b"x" * (MAX_PAYLOAD + 1))
    except ValueError:
        pass
    else:
        raise AssertionError("payload > MAX_PAYLOAD 未被拒绝")


def test_ver_passthrough():
    # CRC 是唯一准入门槛：非 1 版本若 CRC 正确则透传（前向兼容）
    frames = StreamParser().feed(encode_frame(TYPE_LOG, b"x", ver=0x02))
    assert len(frames) == 1 and frames[0].ver == 0x02


def test_names_and_mac_format():
    assert STATE_NAMES == {0: "STILL", 1: "MOTION", 2: "PRESENCE"}
    assert format_mac(MAC1) == "1a:00:00:00:00:00"
    assert format_mac(b"\xAB\xCD\xEF\x01\x02\x03") == "ab:cd:ef:01:02:03"
    ev = decode_payload(StreamParser().feed(build_event(
        Event(MAC1, 1, STATE_MOTION, 87, -6, 1)))[0])
    assert ev.state == STATE_MOTION and STATE_NAMES[ev.state] == "MOTION"


# ---------------------------------------------------------------------------
# 坏帧拒绝 + 其后真帧可恢复
# ---------------------------------------------------------------------------

def _good_pair():
    a = build_event(Event(MAC1, 1, STATE_MOTION, 87, -6, 111))
    b = build_features(Features(MAC1, 222, 3793, 12, 64, -6))
    return a, b


def test_bad_payload_bit_rejected_then_resync():
    good, nxt = _good_pair()
    bad = bytearray(good)
    bad[HEADER_OFF_PAYLOAD] ^= 0x01          # 翻转 payload 首字节 1 bit
    p = StreamParser()
    frames = p.feed(bytes(bad) + nxt)
    assert p.bad_crc == 1, p.stats()
    assert len(frames) == 1 and frames[0].type == TYPE_FEATURES
    assert frames[0].payload == nxt[HEADER_OFF_PAYLOAD:-2]


def test_bad_crc_field_rejected():
    good, nxt = _good_pair()
    bad = bytearray(good)
    bad[-1] ^= 0x80                          # 翻转 CRC 高字节 1 bit
    p = StreamParser()
    frames = p.feed(bytes(bad) + nxt)
    assert p.bad_crc == 1
    assert len(frames) == 1 and frames[0].type == TYPE_FEATURES


def test_bad_ver_rejected():
    good, nxt = _good_pair()
    bad = bytearray(good)
    bad[2] = 0x7F                            # ver 位翻坏（ver 在 CRC 覆盖内）
    p = StreamParser()
    frames = p.feed(bytes(bad) + nxt)
    assert p.bad_crc >= 1 and p.resyncs >= 1
    assert len(frames) == 1 and frames[0].type == TYPE_FEATURES


def test_bad_len_field_resync():
    good, nxt = _good_pair()
    bad = bytearray(good[:4] + struct.pack("<H", 0xFFFF) + good[6:])
    p = StreamParser()
    frames = p.feed(bytes(bad) + nxt)
    assert p.resyncs >= 1 and p.frames_ok == 1
    assert len(frames) == 1 and frames[0].type == TYPE_FEATURES


def test_bad_magic_skipped():
    good, nxt = _good_pair()
    bad = bytearray(good)
    bad[0] = 0x5A                            # 魔数破坏
    p = StreamParser()
    frames = p.feed(bytes(bad) + nxt)
    assert p.bytes_dropped >= len(bad)
    assert len(frames) == 1 and frames[0].type == TYPE_FEATURES


def test_valid_frame_embedded_in_fake_frame_recovered():
    # 假外层帧（CRC 故意弄坏）的 payload 里嵌着一整个真帧 + 其后又一真帧：
    # 外层 CRC 失败 → 前移 1 字节 → 内层真帧应被完整恢复（不吞帧）
    inner = build_event(Event(MAC1, 1, STATE_MOTION, 87, -6, 42))
    outer = bytearray(encode_frame(TYPE_LOG, inner))        # inner 原样作为外层 payload
    outer[-2] ^= 0xFF                                       # 破坏外层 CRC
    tail = build_log("after")
    p = StreamParser()
    frames = p.feed(bytes(outer) + tail)
    assert p.bad_crc == 1, p.stats()
    assert len(frames) == 2, [f.type for f in frames]
    assert frames[0].type == TYPE_EVENT and decode_payload(frames[0]).ts_ms == 42
    assert frames[1].type == TYPE_LOG and decode_payload(frames[1]).text == "after"


# ---------------------------------------------------------------------------
# 失步重同步（大段噪声 + 假帧头）
# ---------------------------------------------------------------------------

def test_desync_garbage_resync():
    rng = random.Random(20261028)
    garbage = bytearray(rng.randbytes(4096))
    # 注入 20 个假帧头：10 个 len 超限（头部废弃路径）、10 个完整假帧（CRC 失败路径）
    for k in range(10):
        off = rng.randrange(0, len(garbage) - 8)
        garbage[off:off + 6] = MAGIC + struct.pack("<BBH", 0x01, 0x10, 0xFFFF)
    for k in range(10):
        off = rng.randrange(0, len(garbage) - 16)
        garbage[off:off + 13] = MAGIC + struct.pack("<BBH", 0x01, 0x10, 5) + rng.randbytes(7)
    good1, good2 = _good_pair()
    stream = bytes(garbage) + good1 + b"\r\nI (1) console noise line\r\n" + good2
    p = StreamParser()
    frames = p.feed(stream)
    assert p.frames_ok == 2, p.stats()
    assert len(frames) == 2, [f.type for f in frames]
    assert frames[0].type == TYPE_EVENT and frames[1].type == TYPE_FEATURES
    assert decode_payload(frames[0]).ts_ms == 111
    assert decode_payload(frames[1]).mean == 3793
    assert p.bad_crc > 0 and p.resyncs > 0 and p.bytes_dropped > 0
    assert p.pending == 0


def test_no_hang_on_oversize_len_alone():
    p = StreamParser()
    assert p.feed(MAGIC + struct.pack("<BBH", 0x01, 0x10, 0xFFFF)) == []
    assert p.resyncs >= 1
    good, _ = _good_pair()
    frames = p.feed(good)                    # 之后真帧仍可解析
    assert len(frames) == 1 and frames[0].type == TYPE_EVENT


# ---------------------------------------------------------------------------
# 分片喂入 / 截断 / 交织
# ---------------------------------------------------------------------------

def test_split_feed_byte_and_chunks():
    rng = random.Random(7)
    states = [STATE_STILL, STATE_MOTION, STATE_PRESENCE, STATE_MOTION]
    objs = []
    for k in range(80):
        kind = k % 6
        if kind == 0:
            objs.append(build_heartbeat(Heartbeat(1000 * k, 3, 200000 + k, k % 5)))
        elif kind == 1:
            objs.append(build_event(Event(MAC1 if k % 2 else MAC2, k % 4,
                                          states[k % 4], k, -(30 + k % 40), 1000 * k)))
        elif kind == 2:
            objs.append(build_features(Features(MAC1, 1000 * k, 3700 + k, k, 2 * k, -6)))
        elif kind == 3:
            objs.append(build_log("log %d" % k))
        elif kind == 4:
            objs.append(build_features_multi(FeaturesMulti(
                k % 4, MAC1 if k % 2 else MAC2, 1000 * k, 3700 + k, k, 2 * k, -6)))
        else:
            objs.append(build_event_multi(EventMulti(
                k % 4, MAC2, k % 4, states[k % 4], k, -(30 + k % 40), 1000 * k)))
    stream = b"".join(objs)
    # 逐字节喂
    p1 = StreamParser()
    got = []
    for i in range(len(stream)):
        got.extend(p1.feed(stream[i:i + 1]))
    assert len(got) == 80 and p1.bad_crc == 0 and p1.bytes_dropped == 0 and p1.resyncs == 0
    for idx, f in enumerate(got):
        assert f.payload == objs[idx][HEADER_OFF_PAYLOAD:-2] and f.type == objs[idx][3]
    # 随机 1~9 字节分片喂
    p2 = StreamParser()
    got2, i = [], 0
    while i < len(stream):
        n = rng.randrange(1, 10)
        got2.extend(p2.feed(stream[i:i + n]))
        i += n
    assert len(got2) == 80 and p2.stats()["frames_ok"] == 80
    assert [f.payload for f in got2] == [f.payload for f in got]


def test_truncated_then_completed():
    good, _ = _good_pair()
    p = StreamParser()
    assert p.feed(good[:5]) == []            # 半个头
    assert p.feed(good[5:10]) == []          # 头齐 payload 缺
    assert p.pending == 10
    frames = p.feed(good[10:])
    assert len(frames) == 1 and frames[0].type == TYPE_EVENT and p.pending == 0


def test_console_text_interleaved():
    t1 = b"I (12345) csi_rx: amp=3793 mean=37.93 std=0.099 rssi=-6\r\n"
    t2 = b"W (23456) wifi: conn retry 1\r\n"
    good1, good2 = _good_pair()
    p = StreamParser()
    frames = p.feed(t1 + good1 + t2 + good2)
    assert len(frames) == 2 and frames[0].type == TYPE_EVENT and frames[1].type == TYPE_FEATURES
    assert p.bytes_dropped == len(t1) + len(t2), p.stats()
    assert p.frames_ok == 2 and p.pending == 0


def test_empty_feed_and_double_frame_single_call():
    p = StreamParser()
    assert p.feed(b"") == []
    a, b = _good_pair()
    frames = p.feed(a + b)
    assert len(frames) == 2 and frames[0].type == TYPE_EVENT and frames[1].type == TYPE_FEATURES


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("[PASS] %s" % name)
        except Exception as exc:  # noqa: BLE001 —— 自测要把任何失败变成可读输出
            failed.append((name, exc))
            print("[FAIL] %s: %r" % (name, exc))
    if failed:
        print("SELFTEST FAILED: %d/%d passed" % (len(tests) - len(failed), len(tests)))
        return 1
    print("SELFTEST OK: %d/%d tests passed" % (len(tests), len(tests)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
