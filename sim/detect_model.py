# -*- coding: utf-8 -*-
"""cis_detect.c 的逐行忠实 Python 移植（黄金模型，sim 仿真核心）。

镜像对象：F:/ESP/projects/ESP32-CIS/ESP32_CIS_V1.0/main/cis_detect.c
逐条对应（行号为 cis_detect.c 当前版本）：
  packet()   ← cis_detect_csi_cb      ：RSSI 突变过滤（|Δ|>6dB 丢包 + 整窗 dirty，
                                         注意 rssi_prev 每包无条件更新——持续型 RSSI
                                         台阶只污染第一窗）、按 MAC 毫单位累加。
  window()   ← cis_detect_radar_cb_ex ：排窗（整除取均值/方差/峰峰值）、centi 换算、
                                         训练收集、滞回 FSM。
关键语义（仿真可信度取决于此，勿"顺手优化"）：
  1. var_milli2 = (uint32)(e2 - mean²)——C 侧 uint64→uint32 截断，Python 用 &0xFFFFFFFF 镜像；
  2. 空窗（cnt==0）与脏窗（RSSI 突变）不产特征、不进 FSM、不累计 quiet_ms，
     但 last_ts_ms 照常刷新；
  3. FSM 比较用未钳制 var_c（>65535 仍可超阈），事件 score 才 clamp_u16；
  4. over=var_c>threshold 为严格大于；N/M 连续计数，任一反向窗清零对方；
  5. quiet_ms 只在非超阈且 state!=IDLE 时按窗间隔累计；fsm_transition 清零；
  6. 训练期间照发特征但不做 FSM 判决；阈值=noise×margin//100，下限钳制，u16 钳制。
"""

from __future__ import annotations

IDLE, MOTION, PRESENCE = 0, 1, 2
STATE_NAMES = {IDLE: "IDLE", MOTION: "MOTION", PRESENCE: "PRESENCE"}


def clamp_u16(v: int) -> int:
    return 0xFFFF if v > 0xFFFF else v


class _Accum:
    """镜像 csi_accum_t（cis_detect.c:44-56）。"""
    __slots__ = ("rssi_prev", "rssi_last", "acc_milli", "sq_acc_milli",
                 "cnt", "min_milli", "max_milli", "dirty", "rssi_jump_drops")

    def __init__(self, first_rssi: int):
        self.rssi_prev = first_rssi
        self.rssi_last = first_rssi
        self.acc_milli = 0
        self.sq_acc_milli = 0
        self.cnt = 0
        self.min_milli = 0
        self.max_milli = 0
        self.dirty = False
        self.rssi_jump_drops = 0


class Feature:
    __slots__ = ("ts_ms", "mean_c", "var_c", "p2p_c", "rssi")

    def __init__(self, ts_ms, mean_c, var_c, p2p_c, rssi):
        self.ts_ms = ts_ms
        self.mean_c = mean_c
        self.var_c = var_c
        self.p2p_c = p2p_c
        self.rssi = rssi

    def __repr__(self):
        return ("Feature(ts=%d mean_c=%d var_c=%d p2p_c=%d rssi=%d)"
                % (self.ts_ms, self.mean_c, self.var_c, self.p2p_c, self.rssi))


class Event:
    __slots__ = ("ts_ms", "mac", "new_state", "score", "rssi", "kind")

    def __init__(self, ts_ms, mac, new_state, score, rssi, kind="state"):
        self.ts_ms = ts_ms
        self.mac = mac
        self.new_state = new_state
        self.score = score
        self.rssi = rssi
        self.kind = kind        # "state" | "calib"

    def __repr__(self):
        if self.kind == "calib":
            return ("Calib(ts=%d thr=%d)" % (self.ts_ms, self.score))
        return ("Event(ts=%d %s->%s score=%d rssi=%d)"
                % (self.ts_ms, self.mac, STATE_NAMES[self.new_state],
                   self.score, self.rssi))


class DetectContext:
    """镜像 det_ctx_t（cis_detect.c:65-89）及其 FSM/训练逻辑（:282-349）。"""

    def __init__(self, mac, N=10, M=25, presence_hold_s=10,
                 threshold=30, margin_pct=300, train_seconds=15, train_min=15):
        self.mac = mac
        self.N = N
        self.M = M
        self.presence_hold_s = presence_hold_s
        self.margin_pct = margin_pct
        self.train_seconds_default = train_seconds
        self.train_min = train_min
        self.state = IDLE
        self.over_cnt = 0
        self.under_cnt = 0
        self.quiet_ms = 0
        self.last_ts_ms = 0
        self.threshold = threshold
        self.rssi = 0
        self.windows = 0
        self.dirty_windows = 0
        # 训练
        self.training = False
        self.train_total_s = 0
        self.train_start_ms = 0
        self.train_var_acc = 0
        self.train_windows = 0
        # 最近窗（记录用）
        self.var_c = 0
        self.mean_c = 0
        self.p2p_c = 0

    # ---- 包级：镜像 cis_detect_csi_cb 的 slot 分支（:184-206） ----------

    def packet(self, acc: _Accum, amp_milli: int, rssi: int):
        if abs(rssi - acc.rssi_prev) > 6:
            acc.dirty = True
            acc.rssi_jump_drops += 1
        else:
            if acc.cnt == 0:
                acc.min_milli = acc.max_milli = amp_milli
            else:
                if amp_milli < acc.min_milli:
                    acc.min_milli = amp_milli
                if amp_milli > acc.max_milli:
                    acc.max_milli = amp_milli
            acc.acc_milli += amp_milli
            acc.sq_acc_milli += amp_milli * amp_milli
            acc.cnt += 1
        acc.rssi_prev = rssi
        acc.rssi_last = rssi

    # ---- 窗级：镜像 cis_detect_radar_cb_ex（:212-350） ------------------

    def window(self, acc: _Accum | None, ts_ms: int):
        """返回 (Feature|None, [Event, ...])。acc 为 None 等价于空窗。"""
        events: list[Event] = []
        self.windows += 1

        mean_milli = var_milli2 = p2p_milli = 0
        rssi_last = 0
        have = dirty = False
        if acc is not None:
            if acc.cnt > 0:
                mean_milli = acc.acc_milli // acc.cnt
                e2 = acc.sq_acc_milli // acc.cnt
                m2 = mean_milli * mean_milli
                if e2 > m2:
                    var_milli2 = (e2 - m2) & 0xFFFFFFFF   # C: (uint32_t) 截断
                p2p_milli = acc.max_milli - acc.min_milli
                have = True
            rssi_last = acc.rssi_last
            dirty = acc.dirty
            acc.acc_milli = acc.sq_acc_milli = 0
            acc.cnt = acc.min_milli = acc.max_milli = 0
            acc.dirty = False

        if not have or dirty:
            if dirty:
                self.dirty_windows += 1
            self.last_ts_ms = ts_ms
            return None, events

        self.mean_c = mean_milli // 10
        self.var_c = var_milli2 // 10000
        self.p2p_c = p2p_milli // 10
        self.rssi = rssi_last
        feat = Feature(ts_ms, self.mean_c, self.var_c, self.p2p_c, self.rssi)

        # 训练收集（:282-308）：照发特征，不做 FSM 判决
        if self.training:
            self.train_var_acc += self.var_c
            self.train_windows += 1
            elapsed = (ts_ms - self.train_start_ms) // 1000
            if elapsed >= self.train_total_s:
                noise = (clamp_u16(self.train_var_acc // self.train_windows)
                         if self.train_windows else self.train_min)
                thr = noise * self.margin_pct // 100
                if thr < self.train_min:
                    thr = self.train_min
                self.threshold = clamp_u16(thr)
                self.training = False
                events.append(Event(ts_ms, self.mac, 0, self.threshold, self.rssi,
                                    kind="calib"))
            self.last_ts_ms = ts_ms
            return feat, events

        # 滞回 FSM（:310-349）
        over = self.var_c > self.threshold
        if over:
            self.over_cnt += 1
            self.under_cnt = 0
        else:
            self.under_cnt += 1
            self.over_cnt = 0
            if self.state != IDLE:
                self.quiet_ms += (ts_ms - self.last_ts_ms) if ts_ms > self.last_ts_ms else 0

        if self.state == IDLE:
            if over and self.over_cnt >= self.N:
                events.append(self._transition(MOTION, ts_ms))
        elif self.state == MOTION:
            if self.under_cnt >= self.M:
                events.append(self._transition(PRESENCE, ts_ms))
        elif self.state == PRESENCE:
            if over and self.over_cnt >= self.N:
                events.append(self._transition(MOTION, ts_ms))
            elif self.quiet_ms >= self.presence_hold_s * 1000:
                events.append(self._transition(IDLE, ts_ms))
        self.last_ts_ms = ts_ms
        return feat, events

    def _transition(self, new_state: int, ts_ms: int) -> Event:
        self.state = new_state
        self.quiet_ms = 0
        return Event(ts_ms, self.mac, new_state,
                     clamp_u16(self.var_c), self.rssi)


class Detector:
    """多 peer 容器：MAC→累加器/上下文，接口对应 cis_detect 对外 API。"""

    def __init__(self, N=10, M=25, presence_hold_s=10, default_threshold=30,
                 margin_pct=300, train_seconds=15, train_min=15):
        self.accs: dict[str, _Accum] = {}
        self.ctxs: dict[str, DetectContext] = {}
        self.cfg = dict(N=N, M=M, presence_hold_s=presence_hold_s,
                        default_threshold=default_threshold,
                        margin_pct=margin_pct, train_seconds=train_seconds,
                        train_min=train_min)
        self.lost_packets = 0     # RSSI 突变丢包总数（镜像 cis_detect_lost_frames）

    def ensure_peer(self, mac: str, threshold: int | None = None):
        if mac not in self.ctxs:
            self.ctxs[mac] = DetectContext(
                mac, threshold=threshold if threshold else self.cfg["default_threshold"],
                **{k: self.cfg[k] for k in
                   ("N", "M", "presence_hold_s", "margin_pct",
                    "train_seconds", "train_min")})
        elif threshold:
            self.ctxs[mac].threshold = threshold
        if mac not in self.accs:
            self.accs[mac] = None          # 首包时以首包 RSSI 初始化（镜像 :172-183）
        return self.ctxs[mac]

    def remove_peer(self, mac: str):
        self.ctxs.pop(mac, None)
        self.accs.pop(mac, None)

    def train_start(self, mac: str, ts_ms: int, seconds: int | None = None) -> bool:
        """镜像 cis_detect_train_start（:439-462），train_start_ms 由调用方给 now_ms。"""
        d = self.ctxs.get(mac)
        if d is None or d.training:
            return False
        d.training = True
        d.train_total_s = seconds or self.cfg["train_seconds"]
        d.train_start_ms = ts_ms
        d.train_var_acc = 0
        d.train_windows = 0
        return True

    def packet(self, mac: str, amp_milli: int, rssi: int):
        """镜像 cis_detect_csi_cb 的 slot 查找+累加（:164-207）。"""
        acc = self.accs.get(mac)
        if acc is None:
            acc = _Accum(rssi)
            self.accs[mac] = acc
        before = acc.rssi_jump_drops
        d = self.ctxs.get(mac)
        if d is not None:
            d.packet(acc, amp_milli, rssi)
        self.lost_packets += acc.rssi_jump_drops - before

    def window(self, mac: str, ts_ms: int):
        """排窗：返回 (Feature|None, [Event, ...])。未注册 peer 静默丢弃。"""
        d = self.ctxs.get(mac)
        if d is None:
            self.accs.pop(mac, None)
            return None, []
        feat, events = d.window(self.accs.get(mac), ts_ms)
        return feat, events
