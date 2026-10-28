# -*- coding: utf-8 -*-
"""包级信道模型：生成 (幅值毫单位, RSSI) 包流 → 按 10Hz 检测窗分窗。

标定依据（台架实测证据，勿随意改动默认值）：
  - 静息幅值均值 ≈ 13.0 raw（M4 peer list: mean=1307 centi-amp，1a:00:00:00:00:00）
  - 静息噪声底 ≈ 227 centi-var（NVS 恢复阈值 681c = 训练 noise×300% → noise≈227c
    → raw 方差≈2.27 → raw std≈1.5）
  - 近场链路（…:01）噪声底 ≈ 6789 centi-var（阈值 20367c ÷ 3 → raw std≈8.2）
  - 幅值饱和上限 ≈ 180 raw（int8 I/Q 单载波幅值上限 √2×127≈180；M4 异常时
    mean=1700c=170 raw、var=1224c=raw std 3.5，即"顶到帽、方差塌缩"形态）
  - RSSI 突变（AGC 增益跳变，esp-csi issue #185）：台阶 ±7~12dB，持续数包
运动模型（假设，灵敏度由场景扫描覆盖，见 run_scenarios.py S1）：
  - 走动 = OU 慢衰落 m(t)（相关时间 τ≈0.3s，模拟多径图样随人移动连续演化）
    + 快噪声。窗内方差来自两者；窗间相关（τ>>窗周期 0.1s）使相邻窗同超阈，
    这正是实机走动能过 N=10 连续判据的原因——若用 i.i.d. 噪声建模，连续判据
    会把检出率打到 0，与 M2/M4 实测矛盾。
  - 慢衰落幅度 σ_m = 2k×σn（k=std 倍率，默认 5，扫描 1.5~15），包络梯形沿 0.2s
拥塞模型：
  - 丢窗概率 p_loss：该检测窗 0 包（esp-radar 缓冲饥饿/队列满），空窗不产特征
    也不进 FSM（与固件一致），UI 观察到的特征率 = 10Hz×(1-p_loss)×(1-P(泊松=0))
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

AMP_MILLI_PER_RAW = 1000


@dataclass
class ChannelCfg:
    base_amp_raw: float = 13.0        # 静息幅值均值（M4 实测 1307c）
    noise_std_raw: float = 1.75       # 静息包幅值 std（模型实测静息 var≈210c≈锚点 227c，
                                      # 含固件整除下偏；标定探针见 sim/out/results.md S0）
    amp_cap_raw: float = 180.0        # int8 I/Q 幅值硬上限
    rssi_base: float = -55.0
    rssi_jitter_std: float = 1.2      # 逐包 AR(1) 抖动 std（dB）
    rssi_jitter_phi: float = 0.9
    rssi_jump_rate: float = 0.0       # AGC 突变次数/秒（0=关闭，S3 扫描）
    rssi_jump_dmin: float = 7.0       # 突变最小幅度（>6 才触发过滤）
    rssi_jump_dmax: float = 12.0
    rssi_jump_hold_pkts: float = 4.0  # 台阶持续包数均值
    pkts_per_window: float = 4.0      # 每检测窗包数均值（esp-radar csi_handle_window≈40ms）
    window_dt_s: float = 0.1          # 检测窗周期（10Hz，CONFIG_CIS_FEATURE_RATE_HZ=10）
    motion_k: float = 5.0             # 走动幅值慢衰落 std 倍率（S1 灵敏度扫描）
    motion_tau_s: float = 0.08        # OU 慢衰落相关时间（探针标定：检出 10/10、
                                      # 时延 1.75s、STILL 2.97s，见 results.md）
    motion_mean_gain: float = 1.15    # 走动均值增益
    motion_ramp_s: float = 0.2        # 包络梯形沿
    loss_prob: float = 0.0            # 拥塞丢窗概率（S2 扫描）
    seed: int = 20261028


@dataclass
class Burst:
    """一次活动（走动/挥手）：[start, start+dur] 秒。"""
    start: float
    dur: float
    k: float | None = None            # 覆盖 cfg.motion_k
    gain: float | None = None


@dataclass
class _RssiState:
    jitter: float = 0.0
    offset: float = 0.0
    hold: int = 0


def _envelope(t: float, bursts: list[Burst], cfg: ChannelCfg):
    """返回 (0..1 包络, 生效 burst)。多 burst 重叠取最大。"""
    env, hit = 0.0, None
    for b in bursts:
        if b.start <= t <= b.start + b.dur:
            e = min(1.0, (t - b.start) / cfg.motion_ramp_s,
                    (b.start + b.dur - t) / cfg.motion_ramp_s)
            if e > env:
                env, hit = e, b
    return env, hit


def build_windows(cfg: ChannelCfg, total_s: float, bursts: list[Burst] = (),
                  amp_steps: list = (), drift_per_s: float = 0.0):
    """生成检测窗序列。

    amp_steps: [(t, factor)] —— 幅值台阶（模拟挪动天线/遮挡后未恢复，无 RSSI 突变）
    drift_per_s: 幅值慢漂移速率（%/秒，模拟温度漂移）
    返回 windows: list[list[(amp_milli:int, rssi:int)]]，长度 = total_s/window_dt_s
    """
    rng = random.Random(cfg.seed)
    rssi_state = _RssiState()
    m_ou = 0.0                        # OU 慢衰落状态（raw）
    sigma_m = 2.0 * cfg.motion_k * cfg.noise_std_raw
    windows = []
    n_ticks = int(round(total_s / cfg.window_dt_s))
    amp_step = 1.0
    step_i = 0
    steps = sorted(amp_steps)

    for i in range(n_ticks):
        t = i * cfg.window_dt_s
        # 幅值台阶（无 RSSI 伴随 → 绕过突变过滤的通道）
        while step_i < len(steps) and t >= steps[step_i][0]:
            amp_step *= steps[step_i][1]
            step_i += 1

        env, hit = _envelope(t, bursts, cfg)

        # 本窗包数：泊松；丢窗 = 0 包
        n_pkts = 0 if rng.random() < cfg.loss_prob else max(
            0, int(rng.gauss(cfg.pkts_per_window, 1.0) + 0.5))

        base = cfg.base_amp_raw * amp_step * (1.0 + drift_per_s * t / 100.0)
        pkts = []
        for _ in range(n_pkts):
            # RSSI：AR(1) 抖动 + AGC 台阶
            rssi_state.jitter = (cfg.rssi_jitter_phi * rssi_state.jitter
                                 + math.sqrt(1 - cfg.rssi_jitter_phi ** 2)
                                 * rng.gauss(0, cfg.rssi_jitter_std))
            if rssi_state.hold > 0:
                rssi_state.hold -= 1
                if rssi_state.hold == 0:
                    rssi_state.offset = 0.0
            elif cfg.rssi_jump_rate > 0 and rng.random() < cfg.rssi_jump_rate * cfg.window_dt_s / cfg.pkts_per_window:
                sign = 1.0 if rng.random() < 0.5 else -1.0
                rssi_state.offset = sign * rng.uniform(cfg.rssi_jump_dmin, cfg.rssi_jump_dmax)
                rssi_state.hold = max(1, int(rng.gauss(cfg.rssi_jump_hold_pkts, 1.5)))
            rssi = int(round(cfg.rssi_base + rssi_state.jitter + rssi_state.offset))

            # 幅值：慢衰落 OU（按包步进，τ 走动期生效）+ 快噪声
            dt_pkt = cfg.window_dt_s / max(1, n_pkts)
            m_ou += -m_ou * dt_pkt / cfg.motion_tau_s \
                + sigma_m * math.sqrt(2.0 * dt_pkt / cfg.motion_tau_s) * rng.gauss(0, 1)
            if env > 0:
                k = hit.k if (hit and hit.k is not None) else cfg.motion_k
                gain = hit.gain if (hit and hit.gain is not None) else cfg.motion_mean_gain
                amp = (base * (1.0 + (gain - 1.0) * env)
                       + env * m_ou + rng.gauss(0, cfg.noise_std_raw))
            else:
                amp = base + rng.gauss(0, cfg.noise_std_raw)
            amp = min(max(amp, 0.0), cfg.amp_cap_raw)   # 硬限幅（int8 I/Q 几何上限）
            pkts.append((int(round(amp * AMP_MILLI_PER_RAW)), rssi))
        windows.append(pkts)
    return windows


def run_detector(detector, windows, mac="1a:00:00:00:00:00", train_at=None,
                 train_s=None, dt_s=0.1):
    """把窗序列喂进 Detector（黄金模型）。

    train_at: t_s —— 该时刻启动训练；train_s: 训练时长（None=默认 15s）。
    返回 dict: features/events 原始序列。
    """
    features, events = [], []
    detector.ensure_peer(mac)          # 对应实机流程：先 peer add 再出数
    for idx, pkts in enumerate(windows):
        ts_ms = int(round(idx * dt_s * 1000))
        for amp_milli, rssi in pkts:
            detector.packet(mac, amp_milli, rssi)
        if train_at is not None and abs(ts_ms - train_at * 1000) < 1:
            detector.train_start(mac, ts_ms, train_s)
        feat, evs = detector.window(mac, ts_ms)
        if feat is not None:
            features.append(feat)
        events.extend(evs)
    return {"features": features, "events": events,
            "n_windows": len(windows), "mac": mac}
