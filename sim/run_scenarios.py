# -*- coding: utf-8 -*-
"""sim 场景批量仿真：S0 标定核对 → S1~S5 参数/降级扫描 → 出图+results.md。

场景与 M5 验收判据的对应：
  S1 阈值裕量扫描      —— 静置误报 ≤2 次/10min；走动检出 ≥9/10
  S2 拥塞丢窗扫描      —— 停走 3s 内 STILL；挥手 3s 可检出
  S3 RSSI 突变过滤     —— 过滤代价（脏窗率）与盲区（无 RSSI 伴随的幅值台阶）
  S4 近场饱和          —— M4 异常（mean 1700c/var 1224c）复现 + 摆放红线推导
  S5 训练污染          —— 清场 SOP 的量化依据 + 重训判定规则
  S6 时序健全性        —— walk-stop-walk 事件序列、PRESENCE→IDLE、二次触发
运行：python run_scenarios.py [--quick]
输出：sim/out/*.png、sim/out/results.md
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent))
from detect_model import Detector, IDLE, MOTION, PRESENCE, STATE_NAMES
from signal_gen import ChannelCfg, Burst, build_windows, run_detector

OUT = Path(__file__).parent / "out"
MAC = "1a:00:00:00:00:00"

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False

results: list[str] = []


def log(line: str = ""):
    print(line)
    results.append(line)


def new_detector(**kw) -> Detector:
    return Detector(**kw)


def feed(detector: Detector, windows, **kw):
    return run_detector(detector, windows, mac=MAC, **kw)


def train_then_run(cfg: ChannelCfg, total_s, bursts=(), train_at=None, train_s=15,
                   seed=None, amp_steps=(), drift_per_s=0.0, **kw):
    """便捷入口：建窗→喂模型（train_at 秒处启动训练）。"""
    if seed is not None:
        cfg = ChannelCfg(**{**cfg.__dict__, "seed": seed})
    wins = build_windows(cfg, total_s, bursts=bursts, amp_steps=amp_steps,
                         drift_per_s=drift_per_s)
    det = new_detector(**kw)
    r = feed(det, wins, train_at=train_at, train_s=train_s)
    r["detector"] = det
    return r


def calib_ts(r):
    """标定完成时刻（ms）；未标定返回 -1。FAR/检出统计只认标定后的事件。"""
    return max((e.ts_ms for e in r["events"] if e.kind == "calib"), default=-1)


# ---- 指标 -------------------------------------------------------------------

def motion_entries(events):
    return [e for e in events if e.kind == "state" and e.new_state == MOTION]


def presence_entries(events):
    return [e for e in events if e.kind == "state" and e.new_state == PRESENCE]


def match_bursts(events, bursts):
    """对每个 burst：是否检出（MOTION 落在 [start, end]）、确认时延、STILL 时延。"""
    out = []
    for b in bursts:
        end = b.start + b.dur
        m = next((e for e in motion_entries(events)
                  if b.start <= e.ts_ms / 1000 <= end), None)
        p = next((e for e in presence_entries(events)
                  if m is not None and e.ts_ms / 1000 > end), None)
        out.append({
            "detected": m is not None,
            "motion_s": (m.ts_ms - b.start * 1000) / 1000 if m else None,
            "still_s": (p.ts_ms - end * 1000) / 1000 if p else None,
        })
    return out


def walk_schedule(rng, total_s, n=10, dur=(4.0, 8.0), gap=(25.0, 45.0)):
    """在 [30, total_s-5] 内摆 n 个互不重叠 burst。"""
    out, t = [], 30.0
    while len(out) < n and t < total_s - 10:
        d = rng.uniform(*dur)
        out.append(Burst(t, d))
        t += d + rng.uniform(*gap)
    return out


# ---- 场景 -------------------------------------------------------------------

def s0_calibration():
    """S0：信道模型静息/走动统计 vs 实测锚点。"""
    log("## S0 信道模型标定核对")
    cfg = ChannelCfg()
    wins = build_windows(cfg, 120.0)
    det = new_detector()
    r = feed(det, wins, train_at=0, train_s=10)
    calib = next(e for e in r["events"] if e.kind == "calib")
    quiet_var = [f.var_c for f in r["features"] if 15 < f.ts_ms / 1000 < 60]
    mean_c = float(np.mean([f.mean_c for f in r["features"]]))
    log("- 静息 50s：var_c 均值=%.0f c（实测锚点≈227c）、P95=%.0f c、mean=%.0f c（锚点 1307c）"
        % (np.mean(quiet_var), np.percentile(quiet_var, 95), mean_c))
    log("- 10s 清场训练 → noise=%d c，阈值=%d c（=noise×300%%，下限 15c）"
        % (calib.score // 3, calib.score))
    # 走动统计
    wins2 = build_windows(cfg, 40.0, bursts=[Burst(15, 15)])
    det2 = new_detector()
    det2.ensure_peer(MAC, threshold=calib.score)
    r2 = feed(det2, wins2)
    mv = [f.var_c for f in r2["features"] if 17 < f.ts_ms / 1000 < 28]
    log("- 走动 15s（k=%.1f）：var_c 均值=%.0f c ≈ 静息×%.1f；对阈值 %d c 的超阈率=%.0f%%"
        % (cfg.motion_k, np.mean(mv), np.mean(mv) / max(1e-9, np.mean(quiet_var)),
           calib.score, 100 * np.mean([v > calib.score for v in mv])))
    log()


def s1_margin_sweep(quick=False):
    """S1：训练裕量 margin 扫描 → 静置误报率 + 走动检出率/时延。"""
    log("## S1 阈值裕量扫描（噪声底≈227c，走动 k=5）")
    reps = 3 if quick else 5
    margins = [200, 250, 300, 350, 400, 500]
    log("| margin | 误报(次/10min, %d 组均值) | 走动检出 | 平均确认时延 s | 平均 STILL 时延 s |"
        % reps)
    log("|---|---|---|---|---|")
    rows = []
    for margin in margins:
        # 误报：reps×10min 清场（只统计标定完成后——默认阈值 30 低于噪声底，
        # 上电未标定期必然误报 MOTION，属固件预期行为，不计入）
        fa = []
        for i in range(reps):
            r = train_then_run(ChannelCfg(seed=1000 + i), 600.0,
                               train_at=1, margin_pct=margin)
            t0 = calib_ts(r)
            fa.append(sum(1 for e in motion_entries(r["events"])
                          if e.ts_ms > t0))
        # 检出：10min 10 burst（同样只认标定后事件）
        bursts = walk_schedule(np.random.default_rng(7), 600.0)
        r = train_then_run(ChannelCfg(seed=2000), 600.0, bursts=bursts,
                           train_at=1, margin_pct=margin)
        t0 = calib_ts(r)
        m = match_bursts([e for e in r["events"]
                          if e.kind == "state" and e.ts_ms > t0], bursts)
        det_n = sum(x["detected"] for x in m)
        lat = [x["motion_s"] for x in m if x["detected"]]
        still = [x["still_s"] for x in m if x["still_s"] is not None]
        row = (margin, np.mean(fa), det_n, len(bursts),
               np.mean(lat) if lat else None, np.mean(still) if still else None)
        rows.append(row)
        missed = [i for i, x in enumerate(m) if not x["detected"]]
        log("| %d%% | %.1f | %d/%d | %s | %s |" % (
            margin, row[1], det_n, len(bursts),
            "%.2f" % row[4] if row[4] is not None else "—",
            "%.2f" % row[5] if row[5] is not None else "—")
            + ("（漏检 burst #%s）" % missed if missed else ""))
    rows = [r for r in rows]
    _plot_margin(rows)
    log()
    return rows


def s2_rate_sweep():
    """S2：拥塞丢窗 → 有效特征率下降对确认时延的影响。"""
    log("## S2 拥塞丢窗扫描（有效特征率 = 10Hz×(1-p)×P(泊松>0)）")
    log("| 丢窗 p | 有效率 Hz | 走动检出 | MOTION 时延 s | STILL 时延 s | 挥手3s 检出 |")
    log("|---|---|---|---|---|---|")
    bursts = walk_schedule(np.random.default_rng(11), 600.0)
    wave = Burst(300.0, 3.0)
    rows = []
    for p in (0.0, 0.3, 0.5, 0.7, 0.85):
        cfg = ChannelCfg(seed=3000, loss_prob=p)
        r = train_then_run(cfg, 600.0, bursts=bursts, train_at=1)
        t0 = calib_ts(r)
        m = match_bursts([e for e in r["events"]
                          if e.kind == "state" and e.ts_ms > t0], bursts)
        det_n = sum(x["detected"] for x in m)
        lat = [x["motion_s"] for x in m if x["detected"]]
        still = [x["still_s"] for x in m if x["still_s"] is not None]
        # 有效特征率（训练后统计）
        feats = [f for f in r["features"]]
        eff_rate = len(feats) / 598.0
        # 挥手 3s（独立段，PRESENCE 复位）
        cfg2 = ChannelCfg(seed=3100, loss_prob=p)
        r2 = train_then_run(cfg2, 360.0, bursts=[wave], train_at=1)
        wv = match_bursts([e for e in r2["events"]
                           if e.kind == "state" and e.ts_ms > calib_ts(r2)],
                          [wave])[0]["detected"]
        row = (p, eff_rate, det_n, len(bursts),
               np.mean(lat) if lat else None,
               np.mean(still) if still else None, wv)
        rows.append(row)
        log("| %.2f | %.1f | %d/%d | %s | %s | %s |" % (
            p, eff_rate, det_n, len(bursts),
            "%.2f" % row[4] if row[4] is not None else "—",
            "%.2f" % row[5] if row[5] is not None else "—",
            "是" if wv else "否"))
    _plot_rate(rows)
    log()
    return rows


def s3_rssi_filter():
    """S3：RSSI 突变过滤的代价与盲区。"""
    log("## S3 RSSI 突变过滤（|Δ|>6dB 丢包+整窗 dirty）")
    log("| 突变率 次/s | 脏窗率 %% | 特征损失 %% | 走动检出 | MOTION 时延 s |")
    log("|---|---|---|---|---|")
    bursts = walk_schedule(np.random.default_rng(13), 600.0)
    rows = []
    for rate in (0.0, 0.1, 0.3, 0.6, 1.0):
        cfg = ChannelCfg(seed=4000, rssi_jump_rate=rate)
        r = train_then_run(cfg, 600.0, bursts=bursts, train_at=1)
        d = r["detector"].ctxs[MAC]
        dirty_rate = d.dirty_windows / d.windows * 100
        feat_loss = (1 - len(r["features"]) / d.windows) * 100
        m = match_bursts([e for e in r["events"]
                          if e.kind == "state" and e.ts_ms > calib_ts(r)], bursts)
        det_n = sum(x["detected"] for x in m)
        lat = [x["motion_s"] for x in m if x["detected"]]
        rows.append((rate, dirty_rate, feat_loss, det_n, len(bursts),
                     np.mean(lat) if lat else None))
        log("| %.1f | %.1f | %.1f | %d/%d | %s |" % (
            rate, dirty_rate, feat_loss, det_n, len(bursts),
            "%.2f" % rows[-1][5] if rows[-1][5] is not None else "—"))
    # 盲区：无 RSSI 伴随的幅值台阶（挪动天线/遮挡后不恢复）
    log("- 盲区测试：静置中 t=300s 幅值 ×1.6 台阶（无 RSSI 突变伴随）×5 组：")
    fas = []
    for i in range(5):
        cfg = ChannelCfg(seed=4100 + i)
        r = train_then_run(cfg, 600.0, train_at=1, amp_steps=[(300.0, 1.6)])
        t0 = calib_ts(r)
        fas.append(sum(1 for e in motion_entries(r["events"]) if e.ts_ms > t0))
    log("  误报 MOTION 组数=%d/5（单窗尖峰被 N=10 连续判据挡住的比例）" % sum(1 for f in fas if f))
    log()
    return rows


def s4_saturation():
    """S4：近场幅值饱和 → 方差塌缩 → 漏检（复现 M4 异常形态）。"""
    log("## S4 近场饱和扫描（base_amp: 13→178 raw，cap=180）")
    log("| base raw (mean_c) | 静息 var_c | 走动 var_c | 走/静比 | 训练阈值 c | 走动检出 |")
    log("|---|---|---|---|---|---|")
    bursts = walk_schedule(np.random.default_rng(17), 600.0, n=8)
    rows = []
    for base in (13.0, 40.0, 80.0, 120.0, 150.0, 172.0):
        cfg = ChannelCfg(seed=5000, base_amp_raw=base)
        r = train_then_run(cfg, 600.0, bursts=bursts, train_at=1)
        calib = next((e for e in r["events"] if e.kind == "calib"), None)
        thr = calib.score if calib else 0
        feats = r["features"]
        # 走动窗：取各 burst 中段
        def burst_var():
            vs = [f.var_c for f in feats
                  if any(b.start + 1 <= f.ts_ms / 1000 <= b.start + b.dur - 1
                         for b in bursts)]
            return np.mean(vs) if vs else float("nan")
        quiet_vs = [f.var_c for f in feats
                    if all(abs(f.ts_ms / 1000 - (b.start + b.dur / 2)) > b.dur / 2 + 2
                           for b in bursts)]
        qv = np.mean(quiet_vs)
        mv = burst_var()
        m = match_bursts([e for e in r["events"]
                          if e.kind == "state" and e.ts_ms > calib_ts(r)], bursts)
        det_n = sum(x["detected"] for x in m)
        rows.append((base, base * 10, qv, mv, mv / max(qv, 1e-9), thr, det_n, len(bursts)))
        log("| %.0f (%.0f c) | %.0f | %.0f | %.1f | %d | %d/%d |" % (
            base, base * 10, qv, mv, mv / max(qv, 1e-9), thr, det_n, len(bursts)))
    _plot_saturation(rows)
    log()
    return rows


def s5_train_contamination(quick=False):
    """S5：训练最后 k 秒有人走动 → 阈值虚高 → 后续漏检。"""
    log("## S5 训练污染扫描（15s 训练，最后 k 秒有走动 k=5）")
    reps = 1 if quick else 2
    log("| 污染 k s | 训练 noise c | 阈值 c（×污染前） | 走动检出 | 误报/10min |")
    log("|---|---|---|---|---|")
    bursts = walk_schedule(np.random.default_rng(19), 600.0)
    rows = []
    for k in (0, 2, 5, 10, 15):
        thrs, det_ns, fas = [], [], []
        for i in range(reps):
            cfg = ChannelCfg(seed=6000 + i)
            wins = build_windows(cfg, 600.0,
                                 bursts=[Burst(25 - k, k)] + bursts)
            det = new_detector()
            r = feed(det, wins, train_at=10, train_s=15)
            calib = next((e for e in r["events"] if e.kind == "calib"), None)
            thrs.append(calib.score if calib else 0)
            # 去掉训练期事件后再匹配（训练从 10s 起，burst 从 30s 起）
            m = match_bursts([e for e in r["events"]
                              if e.kind == "state" and e.ts_ms / 1000 > 28], bursts)
            det_ns.append(sum(x["detected"] for x in m))
            wins_q = build_windows(ChannelCfg(seed=6100 + i), 600.0)
            det_q = new_detector()
            det_q.ensure_peer(MAC, threshold=calib.score if calib else 30)
            rq = feed(det_q, wins_q)
            fas.append(len(motion_entries(rq["events"])))
        noise_c = thrs[0] / 3
        rows.append((k, noise_c, np.mean(thrs), np.mean(thrs) / 681.0,
                     np.mean(det_ns), len(bursts), np.mean(fas)))
        log("| %d | %.0f | %.0f (×%.2f) | %.0f/%d | %.0f |" % (
            k, noise_c, np.mean(thrs), np.mean(thrs) / 681.0,
            np.mean(det_ns), len(bursts), np.mean(fas)))
    _plot_contamination(rows)
    log()
    return rows


def s6_timeline():
    """S6：walk-stop-walk-30s 时序健全性 + 主时序图。"""
    log("## S6 时序健全性（训练→静置 40s→走 10s→停 40s→挥手 3s→静置 20s）")
    cfg = ChannelCfg(seed=7000)
    bursts = [Burst(55.0, 10.0), Burst(110.0, 3.0)]
    r = train_then_run(cfg, 150.0, bursts=bursts, train_at=10, train_s=15)
    log("事件序列：")
    calib_done = False
    for e in r["events"]:
        if e.kind == "calib":
            calib_done = True
            log("  t=%5.1fs  标定完成 threshold=%d c (noise=%d c)"
                % (e.ts_ms / 1000, e.score, e.score // 3))
        else:
            note = "" if calib_done else "  ← 未标定期误报（默认阈值30<噪声底，预期行为）"
            log("  t=%5.1fs  %s (score=%d, rssi=%d)%s"
                % (e.ts_ms / 1000, STATE_NAMES[e.new_state], e.score, e.rssi, note))
    m0 = match_bursts([e for e in r["events"]
                       if e.kind == "state" and e.ts_ms > calib_ts(r)], bursts)
    log("- 走动检出=%s，MOTION 时延=%.2fs，STILL 时延=%.2fs"
        % (m0[0]["detected"], m0[0]["motion_s"], m0[0]["still_s"]))
    log("- 挥手检出=%s" % m0[1]["detected"])
    idle_back = [e for e in r["events"]
                 if e.kind == "state" and e.new_state == IDLE]
    log("- PRESENCE→IDLE 回落次数=%d（每段活动结束静置 10s 后各回落一次："
        "未标定期+走动+挥手 → 预期 3，hold=10s 生效）" % len(idle_back))
    _plot_timeline(r, bursts)
    log()
    return r


# ---- 绘图 -------------------------------------------------------------------

def _plot_margin(rows):
    x = [r[0] for r in rows]
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8))
    ax[0].plot(x, [r[1] for r in rows], "o-")
    ax[0].axhline(2, color="r", ls="--", lw=1, label="M5 判据 ≤2/10min")
    ax[0].set_yscale("symlog")
    ax[0].set_xlabel("训练裕量 margin（%）")
    ax[0].set_ylabel("静置误报 次/10min")
    ax[0].set_title("误报率 vs 裕量")
    ax[0].legend()
    ax[0].grid(alpha=0.3)
    ax[1].plot(x, [100 * r[2] / r[3] for r in rows], "o-", label="走动检出率")
    ax[1].axhline(90, color="r", ls="--", lw=1, label="M5 判据 ≥90%")
    ax[1].set_xlabel("训练裕量 margin（%）")
    ax[1].set_ylabel("检出率 %")
    ax[1].set_title("检出率 vs 裕量")
    ax[1].legend()
    ax[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT / "s1_margin.png", dpi=150)
    plt.close(fig)


def _plot_rate(rows):
    x = [r[1] for r in rows]
    fig, ax = plt.subplots(figsize=(7, 4))
    lat = [r[4] if r[4] is not None else np.nan for r in rows]
    still = [r[5] if r[5] is not None else np.nan for r in rows]
    ax.plot(x, lat, "o-", label="MOTION 确认时延")
    ax.plot(x, still, "s-", label="STILL 确认时延")
    ax.axhline(3.0, color="r", ls="--", lw=1, label="M5 判据 3s（STILL）")
    ax.axhline(1.0, color="orange", ls=":", lw=1, label="设计目标 1s（N=10@10Hz）")
    ax.set_xlabel("有效特征率（Hz）")
    ax.set_ylabel("时延（s）")
    ax.set_title("确认时延 vs 有效特征率（拥塞丢窗）")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT / "s2_rate.png", dpi=150)
    plt.close(fig)


def _plot_saturation(rows):
    fig, ax = plt.subplots(figsize=(7, 4))
    x = [r[1] for r in rows]     # mean_c
    ax.plot(x, [r[2] for r in rows], "o-", label="静息 var_c")
    ax.plot(x, [r[3] for r in rows], "s-", label="走动 var_c")
    ax.set_xlabel("静息幅值均值 mean_c（centi-amp）")
    ax.set_ylabel("var_c（centi-var，对数）")
    ax.set_yscale("log")
    ax2 = ax.twinx()
    ax2.plot(x, [100 * r[6] / r[7] for r in rows], "^--", color="gray",
             label="走动检出率")
    ax2.set_ylabel("检出率 %")
    ax2.set_ylim(0, 105)
    ax.axvline(1224, color="r", ls=":", lw=1)
    ax.annotate("M4 异常实测 mean≈1700c", xy=(1700, ax.get_ylim()[0]),
                xytext=(1700, 10), fontsize=8, color="r")
    lines = ax.get_lines() + ax2.get_lines()
    ax.legend(lines, [l.get_label() for l in lines], loc="center left", fontsize=9)
    ax.set_title("近场饱和：幅值逼近 int8 上限 → 方差塌缩 → 漏检")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT / "s4_saturation.png", dpi=150)
    plt.close(fig)


def _plot_contamination(rows):
    fig, ax = plt.subplots(figsize=(7, 4))
    x = [r[0] for r in rows]
    ax.plot(x, [r[2] for r in rows], "o-", label="训练所得阈值")
    ax.axhline(681, color="g", ls=":", lw=1, label="干净训练阈值 681c")
    ax.set_xlabel("训练期污染秒数 k（15s 训练，最后 k 秒有人走动）")
    ax.set_ylabel("阈值（centi-var）")
    ax2 = ax.twinx()
    ax2.plot(x, [100 * r[4] / r[5] for r in rows], "s--", color="gray",
             label="走动检出率")
    ax2.set_ylabel("检出率 %")
    ax2.set_ylim(0, 105)
    lines = ax.get_lines() + ax2.get_lines()
    ax.legend(lines, [l.get_label() for l in lines], fontsize=9)
    ax.set_title("训练污染：清场 SOP 的量化依据")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT / "s5_contamination.png", dpi=150)
    plt.close(fig)


def _plot_timeline(r, bursts):
    feats = r["features"]
    t = np.array([f.ts_ms / 1000 for f in feats])
    var = np.array([f.var_c for f in feats], dtype=float)
    state = np.array([_state_at(r["events"], f.ts_ms) for f in feats], dtype=float)
    thr = next((e.score for e in r["events"] if e.kind == "calib"), 30)

    fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(10, 5.2), sharex=True,
                                   height_ratios=[3, 1])
    ax0.plot(t, var, lw=0.9, color="#2062af", label="窗口方差 var_c")
    ax0.axhline(thr, color="r", ls="--", lw=1.2, label="阈值 %dc（训练 noise×300%%）" % thr)
    for b in bursts:
        ax0.axvspan(b.start, b.start + b.dur, color="orange", alpha=0.18)
    ax0.set_yscale("log")
    ax0.set_ylabel("var_c（centi-var，对数）")
    ax0.set_title("黄金模型时序仿真：训练→静置→走动→静止→挥手（S6）")
    ax0.legend(fontsize=9)
    ax0.grid(alpha=0.3)
    ax1.step(t, state, where="post", lw=1.4, color="#3a8f3a")
    ax1.set_yticks([0, 1, 2])
    ax1.set_yticklabels(["IDLE", "MOTION", "PRESENCE"])
    ax1.set_xlabel("时间（s）")
    ax1.set_ylim(-0.3, 2.3)
    ax1.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT / "s6_timeline.png", dpi=150)
    plt.close(fig)


def _state_at(events, ts_ms):
    st = IDLE
    for e in events:
        if e.ts_ms > ts_ms:
            break
        if e.kind == "state":       # 跳过 calib 等非状态事件
            st = e.new_state
    return st


# ---- 主流程 -----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    log("# CIS 检测算法仿真结果（黄金模型 = cis_detect.c 忠实移植）")
    log()
    log("参数锚点：噪声底 227c、静息幅值 1307c、走动 k=5、窗均 4 包、10Hz（依据见 signal_gen.py 头注）。")
    log("随机种子固定，可复现。运行模式：%s" % ("quick" if args.quick else "full"))
    log()
    s0_calibration()
    s6_timeline()
    s1_margin_sweep(quick=args.quick)
    s2_rate_sweep()
    s3_rssi_filter()
    s4_saturation()
    s5_train_contamination(quick=args.quick)

    (OUT / "results.md").write_text("\n".join(results), encoding="utf-8")
    print("\n写出 %s 与 PNG 图 %d 张" % (OUT / "results.md",
                                        len(list(OUT.glob("*.png")))))


if __name__ == "__main__":
    main()
