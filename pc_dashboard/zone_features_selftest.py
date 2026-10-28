"""zone_features_selftest.py — zone_features 纯逻辑自测（无 Qt 依赖）。

用法：python zone_features_selftest.py   （全过输出 PASS，任一断言失败抛 AssertionError）
"""

from __future__ import annotations

import csv
import math
import random
import tempfile
import os

from zone_features import (BreathPreview, DwellDetector, TrajectoryAnalyzer,
                           describe_direction, load_var_series, region_name)


def test_direction_and_region():
    assert describe_direction(0.10, 0.00) == "自西向东"
    assert describe_direction(-0.10, 0.00) == "自东向西"
    assert describe_direction(0.00, 0.10) == "自北向南"
    assert describe_direction(0.00, -0.10) == "自南向北"
    assert describe_direction(0.001, 0.0) is None
    assert region_name(0.5, 0.5) == "中心区"
    assert region_name(0.1, 0.1) == "西北"
    assert region_name(0.9, 0.9) == "东南"
    print("ok  direction/region")


def test_trajectory():
    ta = TrajectoryAnalyzer(side_m=1.4)
    t0 = 1000.0
    # 8 秒自西向东匀速走 1.4m（0.2 → 1.2），步进 0.4s
    for i in range(21):
        ta.feed(t0 + i * 0.4, 0.2 + i * 0.05, 0.5)
    assert ta.direction() == "自西向东"
    sp = ta.speed_mps()
    assert sp is not None and 0.14 < sp < 0.21, sp     # 1.4m/8s = 0.175
    assert len(ta.trail()) <= ta.max_points
    # 8s 静默后再出现 → reentry 事件
    ta.feed_lost(t0 + 20.0)
    ta.feed(t0 + 28.0, 0.5, 0.5)
    ev = ta.pop_events()
    assert len(ev) == 1 and ev[0]["kind"] == "reentry" and ev[0]["region"] == "中心区"
    assert ta.pop_events() == []
    print("ok  trajectory (direction/speed/reentry)")


def test_dwell():
    dd = DwellDetector(dwell_s=8.0)
    t0 = 2000.0
    for i in range(12):                                 # 东北驻留 12s
        dd.feed(t0 + i, 0.8, 0.1)
    ev = dd.pop_events()
    assert len(ev) == 1 and ev[0]["kind"] == "dwell" and ev[0]["region"] == "东北"
    assert dd.pop_events() == []                        # 不重复报
    for i in range(12, 22):                             # 换区西北再驻留 10s
        dd.feed(t0 + i, 0.1, 0.1)
    ev = dd.pop_events()
    assert len(ev) == 1 and ev[0]["region"] == "西北"
    print("ok  dwell (single report per stay, region switch)")


def test_breath_sine_vs_noise():
    bp = BreathPreview()
    rng = random.Random(42)
    fs, dur, f0 = 10.0, 60.0, 0.25                      # 呼吸 0.25Hz（15 次/分）
    n = int(fs * dur)
    times = [i / fs for i in range(n)]
    sig = [30.0 * math.sin(2 * math.pi * f0 * t) + rng.gauss(0, 5.0) for t in times]
    r = bp.analyze(times, sig)
    assert r["confident"], r
    assert abs(r["freq_hz"] - f0) < 0.02, r
    # 纯噪声：不报 confident
    rng2 = random.Random(7)
    noise = [rng2.gauss(0, 5.0) for _ in times]
    r2 = bp.analyze(times, noise)
    assert not r2["confident"] and r2["snr"] < r["snr"], r2
    # 样本不足（<2 个 fmin 周期）
    r3 = bp.analyze(times[:50], sig[:50])
    assert not r3["confident"]
    print("ok  breath preview (0.25Hz found, noise rejected, short input rejected)")


def test_csv_loader():
    rows = [["ts_iso", "observer", "mac", "name", "type", "state", "score",
             "mean", "var", "p2p", "rssi"],
            ["2026-10-29T10:00:00.000000", "S3", "1A:00:00:00:00:00", "TX1",
             "FEATURES", "", "", "100", "250", "300", "-50"],
            ["2026-10-29T10:00:00.100000", "S3", "1A:00:00:00:00:00", "TX1",
             "EVENT", "MOTION", "999", "", "", "", "-50"],
            ["2026-10-29T10:00:00.200000", "TX1", "1A:00:00:00:00:03", "S3",
             "FEATURES", "", "", "100", "300", "320", "-52"]]
    fd, path = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        t, v = load_var_series(path)                    # 全部 FEATURES
        assert len(t) == 2 and v == [250.0, 300.0], (t, v)
        t2, v2 = load_var_series(path, observer="S3", mac_suffix="00")
        assert len(t2) == 1 and v2 == [250.0]
        t3, _ = load_var_series(path, mac_suffix="03")  # 反向链路（observer=TX1）
        assert len(t3) == 1
    finally:
        os.unlink(path)
    print("ok  csv loader (FEATURES filter / observer+mac filter)")


if __name__ == "__main__":
    test_direction_and_region()
    test_trajectory()
    test_dwell()
    test_breath_sine_vs_noise()
    test_csv_loader()
    print("PASS")
