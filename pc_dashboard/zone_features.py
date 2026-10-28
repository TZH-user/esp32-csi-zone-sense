"""zone_features.py — 区域特征纯逻辑层（功能接入准备，2026-10-29）

上位机"下一步功能"的算法底座：轨迹/方向、驻留检测、出入再入场计数、
呼吸幅域预览。全部纯 Python（不依赖 Qt），既可被 UI 回路喂入，也可直接
吃 CSV 会话离线回放，配套 zone_features_selftest.py 自测。
功能清单与接入点说明见 docs/功能清单与路线.md。

坐标口径与 ZoneMapWidget 一致：归一化 (x, y) ∈ [0,1]，x 向东 y 向南
（y=0 为北），1.0 ≈ 场地边长 side_m。
"""

from __future__ import annotations

import csv
import math
from collections import deque
from datetime import datetime


# ---- 共享几何 ----------------------------------------------------------------

def region_name(x: float, y: float) -> str:
    """区域九宫格命名（中心区 + 西北/东北/西南/东南四象限）。"""
    if 0.35 < x < 0.65 and 0.35 < y < 0.65:
        return "中心区"
    ns = "北" if y < 0.5 else "南"
    ew = "西" if x < 0.5 else "东"
    return ew + ns


def describe_direction(dx: float, dy: float, min_step: float = 0.03) -> str | None:
    """归一化位移 → 方位短语（主轴），位移过小返回 None。"""
    if math.hypot(dx, dy) < min_step:
        return None
    if abs(dx) >= abs(dy):
        return "自西向东" if dx > 0 else "自东向西"
    return "自北向南" if dy > 0 else "自南向北"


# ---- 轨迹：尾迹 / 方向 / 速度 / 再入场 ----------------------------------------

class TrajectoryAnalyzer:
    """吃"确信定位点"序列（ZoneMap 质心 total≥门限时喂入），产出：
    trail() 绘图尾迹、direction() 当前走向、speed_mps() 步速、
    pop_events() 再入场事件（静默 gap 后重新出现 + 定向）。"""

    def __init__(self, side_m: float = 1.4, max_points: int = 24,
                 reentry_gap_s: float = 8.0, dir_window_s: float = 8.0):
        self.side_m = side_m
        self.max_points = max_points
        self.reentry_gap_s = reentry_gap_s
        self.dir_window_s = dir_window_s
        self._pts: deque[tuple[float, float, float]] = deque(maxlen=max_points)  # (ts,x,y)
        self._last_ts: float | None = None
        self._events: list[dict] = []

    def feed(self, ts: float, x: float, y: float):
        if self._last_ts is not None and (ts - self._last_ts) >= self.reentry_gap_s:
            self._events.append({"kind": "reentry", "ts": ts,
                                 "x": x, "y": y, "region": region_name(x, y)})
        self._last_ts = ts
        if self._pts:
            lx, ly = self._pts[-1][1], self._pts[-1][2]
            if math.hypot(x - lx, y - ly) < 0.004:      # 抖动去重
                self._pts[-1] = (ts, x, y)
                return
        self._pts.append((ts, x, y))

    def feed_lost(self, ts: float):
        """位置失置信（淡出）——只记时刻，不清尾迹（尾迹随 maxlen 滚动）。"""
        self._last_ts = ts

    def clear(self):
        self._pts.clear()

    def trail(self) -> list[tuple[float, float]]:
        return [(x, y) for _, x, y in self._pts]

    def direction(self) -> str | None:
        """窗口内首尾确信点的合成位移方向（不足两个远距点返回 None）。"""
        if len(self._pts) < 2:
            return None
        t0 = self._pts[-1][0] - self.dir_window_s
        old = next((p for p in self._pts if p[0] >= t0), self._pts[0])
        dx = self._pts[-1][1] - old[1]
        dy = self._pts[-1][2] - old[2]
        return describe_direction(dx, dy)

    def speed_mps(self) -> float | None:
        """相邻远距点（≥0.06 归一化位移）的平均步速，m/s。"""
        for i in range(len(self._pts) - 1, 0, -1):
            t1, x1, y1 = self._pts[i]
            for j in range(i - 1, -1, -1):
                t0, x0, y0 = self._pts[j]
                d = math.hypot(x1 - x0, y1 - y0)
                if d >= 0.06:
                    dt = max(t1 - t0, 1e-6)
                    return min(d * self.side_m / dt, 5.0)
        return None

    def pop_events(self) -> list[dict]:
        out, self._events = self._events, []
        return out


# ---- 驻留检测 ------------------------------------------------------------------

class DwellDetector:
    """同一区域连续停留 ≥ dwell_s 且漂移小于 move_tol → 一次 dwell 事件
    （每段只报一次）；换区或人员失联（> lost_grace_s）即重置。"""

    def __init__(self, dwell_s: float = 8.0, move_tol: float = 0.12,
                 lost_grace_s: float = 6.0):
        self.dwell_s = dwell_s
        self.move_tol = move_tol
        self.lost_grace_s = lost_grace_s
        self._region: str | None = None
        self._enter_ts: float | None = None
        self._anchor: tuple | None = None
        self._last_ts: float | None = None
        self._reported = False
        self._events: list[dict] = []

    def feed(self, ts: float, x: float, y: float):
        reg = region_name(x, y)
        if self._last_ts is not None and ts - self._last_ts > self.lost_grace_s:
            self._reset()
        self._last_ts = ts
        if reg != self._region:
            self._region, self._enter_ts = reg, ts
            self._anchor, self._reported = (x, y), False
        elif self._anchor and math.hypot(x - self._anchor[0], y - self._anchor[1]) > self.move_tol:
            self._anchor = (x, y)                       # 区内漂移：重锚不重报
        if (self._enter_ts is not None and not self._reported
                and ts - self._enter_ts >= self.dwell_s):
            self._reported = True
            self._events.append({"kind": "dwell", "ts": ts, "region": reg,
                                 "dwell_s": round(ts - self._enter_ts, 1)})

    def feed_lost(self, ts: float):
        if self._last_ts is not None and ts - self._last_ts > self.lost_grace_s:
            self._reset()

    def _reset(self):
        self._region = self._enter_ts = self._anchor = None
        self._reported = False

    def pop_events(self) -> list[dict]:
        out, self._events = self._events, []
        return out


# ---- 呼吸幅域预览（P2 支线的离线工具） ------------------------------------------

class BreathPreview:
    """对单链路 var（centi-var）时间序列做呼吸带谱峰预览。
    当前固件只输出幅度域特征，此为"能不能不换固件就看到呼吸"的判据工具：
    谱峰 SNR≥snr_min 且落在呼吸带内才报 confident；用于决定 P2 是否值得上相位域。"""

    def __init__(self, fmin: float = 0.1, fmax: float = 0.5, fs: float = 2.0,
                 snr_min: float = 3.0):
        self.fmin, self.fmax, self.fs, self.snr_min = fmin, fmax, fs, snr_min

    def analyze(self, times: list[float], values: list[float]) -> dict:
        """times 单调增秒；values 同长。返回 {freq_hz, period_s, snr, confident,
        n_samples, duration_s}；样本不足以覆盖 2 个最低频周期时 confident=False。"""
        out = {"freq_hz": None, "period_s": None, "snr": 0.0,
               "confident": False, "n_samples": len(values),
               "duration_s": round(times[-1] - times[0], 1) if len(times) >= 2 else 0.0}
        if len(values) < int(2 / self.fmin * self.fs):   # ≥2 个 fmin 周期
            return out
        u, v = self._resample(times, values)
        if len(v) < 8:
            return out
        v = self._detrend(v)
        spec = self._power(u, v)
        band = [(f, p) for f, p in spec if self.fmin <= f <= self.fmax]
        if not band:
            return out
        f_peak, p_peak = max(band, key=lambda fp: fp[1])
        floor = sorted(p for _, p in band)[max(0, len(band) // 2)]   # 带内中位地板
        snr = math.sqrt(p_peak / floor) if floor > 0 else 0.0
        out.update(freq_hz=round(f_peak, 4), period_s=round(1.0 / f_peak, 2),
                   snr=round(snr, 2), confident=snr >= self.snr_min)
        return out

    # -- 内部：均值重采样 → 去均值去线性趋势 → 周期图 ---------------------------

    def _resample(self, times, values):
        step = 1.0 / self.fs
        t0, t1 = times[0], times[-1]
        n = int((t1 - t0) / step) + 1
        acc = [[0.0, 0.0, 0] for _ in range(n)]          # [Σv, Σw, k] w=时间权重
        for t, v in zip(times, values):
            pos = (t - t0) / step
            i = min(int(pos), n - 1)
            w = 1.0 - (pos - i)
            acc[i][0] += v * w; acc[i][1] += w; acc[i][2] += 1
            if i + 1 < n:
                acc[i + 1][0] += v * (1 - w); acc[i + 1][1] += (1 - w); acc[i + 1][2] += 1
        u, v_out = [], []
        for i, (sv, sw, k) in enumerate(acc):
            if sw > 0:
                u.append(i * step); v_out.append(sv / sw)
        return u, v_out

    @staticmethod
    def _detrend(v):
        n = len(v)
        mean = sum(v) / n
        if n < 3:
            return [x - mean for x in v]
        # 最小二乘去线性趋势
        sx = sum(range(n)); sxx = sum(i * i for i in range(n))
        sy = sum(v); sxy = sum(i * x for i, x in enumerate(v))
        denom = n * sxx - sx * sx
        slope = (n * sxy - sx * sy) / denom if denom else 0.0
        inter = (sy - slope * sx) / n
        return [x - (inter + slope * i) for i, x in enumerate(v)]

    def _power(self, u, v):
        n = len(u)
        # Hann 窗降低泄漏
        w = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]
        vw = [v[i] * w[i] for i in range(n)]
        fbin = 1.0 / (n / self.fs)
        out = []
        for k in range(1, n // 2):
            f = k * fbin
            if f > self.fmax * 1.5:
                break
            re = sum(vw[i] * math.cos(2 * math.pi * f * u[i]) for i in range(n))
            im = sum(vw[i] * math.sin(2 * math.pi * f * u[i]) for i in range(n))
            out.append((f, re * re + im * im))
        return out


def load_var_series(csv_path: str, observer: str | None = None,
                     mac_suffix: str | None = None,
                     t0: float | None = None, dur_s: float | None = None):
    """CSV 会话 → (times, vars)。链路过滤=observer 名 + MAC 末字节（如 "AE"）。
    只取 FEATURES 行；返回绝对 epoch 秒时间戳（用 ts_iso 解析）。"""
    times: list[float] = []
    vals: list[float] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("type") != "FEATURES" or not row.get("var"):
                continue
            if observer and row.get("observer", "") != observer:
                continue
            if mac_suffix and row.get("mac", "").replace(":", "")[-2:] != mac_suffix.upper():
                continue
            t = datetime.fromisoformat(row["ts_iso"]).timestamp()
            if t0 is not None and t < t0:
                continue
            if dur_s is not None and t > t0 + dur_s:
                break
            times.append(t)
            try:
                vals.append(float(row["var"]))
            except ValueError:
                times.pop()
    return times, vals


# ---- 命令行：呼吸带谱峰预览（离线吃 CSV 会话） ----------------------------------

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="呼吸幅域预览：对 CSV 会话单链路 var 序列做呼吸带谱峰分析")
    ap.add_argument("csv", help="data/<会话>/cis_log.csv 路径")
    ap.add_argument("--observer", default=None, help="observer 节点名，如 S3")
    ap.add_argument("--mac", default=None, help="源 MAC 末字节，如 00")
    ap.add_argument("--t0", type=float, default=None, help="起始 epoch 秒（缺省=全程）")
    ap.add_argument("--dur", type=float, default=None, help="时长秒")
    args = ap.parse_args()
    ts, vs = load_var_series(args.csv, observer=args.observer,
                             mac_suffix=args.mac, t0=args.t0, dur_s=args.dur)
    print("样本 %d 个，跨 %.1f s" % (len(vs), ts[-1] - ts[0] if len(ts) >= 2 else 0))
    if not vs:
        raise SystemExit("无 FEATURES 样本：检查 --observer/--mac 过滤")
    r = BreathPreview().analyze(ts, vs)
    print("谱峰 %(freq_hz)s Hz（周期 %(period_s)s s）  SNR=%(snr)s  "
          "confident=%(confident)s" % r)
    print("判读：confident=True → 值得上相位域（P2 支线）；False → 幅域看不到呼吸，"
          "先检查录波是否为 30s 静息、人员离链路 1~2m。")
