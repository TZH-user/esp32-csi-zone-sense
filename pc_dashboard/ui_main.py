"""CIS 桌面监控主界面（PySide6 + pyqtgraph）—— IMPLEMENTATION_PLAN.md §6 ui_main.py。

布局：
    顶栏   数据源选择（串口 COM@2M / UDP :3333 / 回放 CSV / 内置模拟）+ 连接/断开、
           记录开关、帧率与丢帧统计（队列丢/固件丢/CRC 错/环丢字节）、心跳信息
    左列   区域卡片：大状态灯（灰=无数据 绿=STILL 红=MOTION 橙=PRESENCE）+ score
           + 最近事件 + RSSI/var + 标定按钮——点击 15s 倒计时（留人离场时间，§7 SOP）
           后自动发 `train <mac> 15`，等 0x30 帧回填阈值/噪声底
    中央   pyqtgraph 每 peer var 特征曲线 + 事件时刻竖线（颜色=状态）
    底部   事件表（时间/区域/MAC/事件/score/RSSI）+ REPL 命令框 + 日志回显

数据流：Source（SerialSource/UdpSource/CsvReplayer/SimSource，鸭子类型：
frame_queue + start/stop/stats，可选 send_line）→ UI 线程 QTimer 排空
frame_queue → store.frame_to_record → 分发 Store/卡片/曲线/事件表。
命令通道只在串口/模拟源存在（UDP 单向、CSV 回放无通道）。

用法：
    python ui_main.py                          # 串口 COM3@2M
    python ui_main.py --auto-scan --auto-start --auto-record   # 一键：扫描+连接+记录
    python ui_main.py --source udp --udp-port 3333
    python ui_main.py --source sim             # 无硬件演示
    python ui_main.py --source replay --replay-file ..\\data\\xxx\\cis_log.csv
"""

from __future__ import annotations

import math
import os
import sys
import time
import queue
import argparse
import random
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")   # 强制 pyqtgraph 绑 PySide6

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QAbstractItemView,
)

import protocol
from store import Record, Store, default_zone_name, frame_to_record
from udp_source import UdpSource
from serial_source import SerialSource
from self_test import ProbeResult, _clean_text

import pyqtgraph as pg

pg.setConfigOptions(antialias=False, background="w", foreground="k")

# 状态 → 状态灯颜色 / 事件竖线颜色
STATE_COLORS = {None: "#9e9e9e", 0: "#2eb82e", 1: "#e0311c", 2: "#e6a300"}
PEER_COLORS = ["#d62728", "#1f77b4", "#2ca02c", "#9467bd",
               "#ff7f0e", "#17becf", "#8c564b", "#e377c2"]
EVENT_LINE_STYLES = {0: Qt.PenStyle.SolidLine, 1: Qt.PenStyle.DashLine,
                     2: Qt.PenStyle.DotLine}
CURVE_MAX_POINTS = 3600         # ~6min @10Hz/peer，deque 自动丢最老
EVENT_LINE_MAX = 300
TABLE_MAX_ROWS = 500
STALE_AFTER_S = 10.0            # 卡片超此时长无该 MAC 数据 → 状态灯置灰
CALIB_COUNTDOWN_S = 15          # §6：单卡标定倒计时秒数
CALIB_TEST_COUNTDOWN_S = 10     # 全量标定测试倒计时秒数
CALIB_WAIT_0X30_S = 45          # 等 0x30 的超时


# ===========================================================================
# 模拟数据源（无硬件演示 / ui_smoke 冒烟；与 SerialSource 同鸭子接口）
# ===========================================================================

class SimSource:
    """两个 peer 的心跳 1Hz + 特征流 10Hz + 周期性运动事件；回应 train 命令
    0.8s 后回 0x30（阈值=2×噪声底）。随机序列固定 seed，冒烟可复现。"""

    def __init__(self, peers: int = 3, feature_hz: float = 10.0,
                 event_period: float = 6.0, seed: int = 7):
        """SimSource：模拟 CSI 数据流。

        peers：模拟的 peer 数量（默认 3，匹配实际硬件配置：TX1/TX2/TX3）。
               每个 peer 的 source MAC 为 1a:00:00:00:00:01 ~ 1a:00:00:00:00:0n。
        """
        self.frame_queue: "queue.Queue[protocol.Frame]" = queue.Queue(maxsize=5000)
        self._sp = protocol.StreamParser()   # 编码字节 → Frame 对象（与串口源同型）
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rng = random.Random(seed)
        self._peers = [bytes([0x1A, 0x00, 0x00, 0x00, 0x00, i + 1])
                       for i in range(peers)]
        self._hz = feature_hz
        self._period = event_period
        self._frames_ok = 0
        self._queue_dropped = 0
        self._last_error = None

    def start(self):
        if self._thread is not None:
            raise RuntimeError("SimSource already started")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cis-sim", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
            self._thread = None

    def send_line(self, text: str) -> bool:
        """模拟 REPL：train <mac> <sec> → 0.8s 后 0x30；其他命令回 LOG OK。"""
        text = text.strip()
        parts = text.split()
        try:
            if parts and parts[0] == "train" and len(parts) >= 2:
                mac = bytes(int(b, 16) for b in parts[1].split(":"))
                dur = int(parts[2]) if len(parts) >= 3 else 15
                threading.Timer(0.8, self._emit_calib, args=(mac, dur)).start()
                return True
            if parts:
                self._put(protocol.encode_frame(
                    protocol.TYPE_LOG, ("OK: %s" % text).encode()[:200]))
        except Exception as exc:
            self._last_error = exc
            return False
        return True

    def stats(self) -> dict:
        return {"frames_ok": self._frames_ok, "bad_crc": 0, "resyncs": 0,
                "bytes_dropped": 0, "queue_dropped": self._queue_dropped,
                "sim": True}

    # ---- 内部 --------------------------------------------------------------

    def _put(self, frame: bytes):
        """编码帧字节 → 解析回 Frame 对象再入队（与 SerialSource 契约一致）。"""
        for fr in self._sp.feed(frame):
            self._frames_ok += 1
            try:
                self.frame_queue.put_nowait(fr)
            except queue.Full:
                self._queue_dropped += 1

    def _emit_calib(self, mac: bytes, dur: int):
        noise = 30 + self._rng.randint(0, 8)
        self._put(protocol.build_calib(protocol.CalibResult(
            mac=mac, train_duration=dur, threshold=noise * 2, noise_floor=noise)))

    def _loop(self):
        t_start = time.monotonic()
        next_hb = 0.0
        next_feat = {mac: 0.0 for mac in self._peers}
        motion_until = {mac: -1.0 for mac in self._peers}      # <now ⇒ 静止
        next_event = {mac: 2.0 + 1.7 * i + self._rng.uniform(0, 1.0)
                      for i, mac in enumerate(self._peers)}
        next_state_frame = {mac: 1e9 for mac in self._peers}
        phase = {mac: self._rng.uniform(0, 6.28) for mac in self._peers}
        while not self._stop.wait(0.02):
            now = time.monotonic() - t_start
            if now >= next_hb:
                next_hb = now + 1.0
                self._put(protocol.build_heartbeat(protocol.Heartbeat(
                    uptime_ms=int(now * 1000), peer_count=len(self._peers),
                    free_heap=200000 - int(now), dropped_frames=0)))
            for idx, mac in enumerate(self._peers):
                if now >= next_feat[mac]:
                    next_feat[mac] = now + 1.0 / self._hz
                    motion = now < motion_until[mac]
                    var = (self._rng.randint(300, 1200) if motion else
                           22 + int(8 * (1 + math.sin(now * 0.7 + phase[mac])))
                           + self._rng.randint(0, 6))
                    self._put(protocol.build_features(protocol.Features(
                        mac=mac, ts_ms=int(now * 1000),
                        mean=38 + self._rng.randint(-2, 2),
                        var=var, p2p=(self._rng.randint(60, 200) if motion
                                      else self._rng.randint(2, 12)),
                        rssi=-55 - 3 * idx + self._rng.randint(-1, 1))))
                if now >= next_event[mac]:
                    next_event[mac] = now + self._period + self._rng.uniform(0, 2.0)
                    motion_until[mac] = now + 2.2
                    next_state_frame[mac] = now
                if motion_until[mac] > 0 and now >= next_state_frame[mac]:
                    motion = now < motion_until[mac]
                    self._put(protocol.build_event(protocol.Event(
                        mac=mac, name_id=idx + 1,
                        state=1 if motion else 0,
                        score=(self._rng.randint(80, 900) if motion
                               else self._rng.randint(5, 28)),
                        rssi=-58, ts_ms=int(now * 1000))))
                    if motion:
                        next_state_frame[mac] = now + 0.5      # 运动期 2Hz 重发
                    else:
                        next_state_frame[mac] = 1e9            # STILL 收尾帧


# ===========================================================================
# 区域卡片
# ===========================================================================

# 状态中文名称 + 颜色映射
STATE_NAMES_CN = {
    0: "空闲",    # IDLE
    1: "运动",    # MOTION
    2: "存在",    # PRESENCE
}

# 状态指示灯颜色：空闲=灰色, 运动=红色, 存在=黄色
STATE_COLORS_CN = {
    0: "#d9d9d9",  # 空闲 - 灰色
    1: "#ff4d4f",  # 运动 - 红色
    2: "#faad14",  # 存在 - 黄色
}

# MAC → 设备名称映射（按源 MAC 后字节）
MAC_DEVICE_MAP = {
    "00": ("TX1", "经典#1 TX1"),     # 1a:00:00:00:00:00
    "01": ("TX2", "C3 TX2"),         # 1a:00:00:00:00:01
    "02": ("TX3", "经典#2 TX3"),     # 1a:00:00:00:00:02
}

class ZoneCard(QFrame):
    """左列卡片：大状态灯 + 设备名/MAC + score + 最近事件 + RSSI/var
    + 标定按钮（倒计时逻辑由 Dashboard 统一驱动，本卡只做显示）。"""

    calib_requested = Signal(str)          # mac
    card_clicked = Signal(str)             # mac (卡片被点击时发射)

    def __init__(self, mac: str, name: str, parent=None):
        super().__init__(parent)
        self.mac = mac
        self.zone_name = name
        self._state = None                 # 最近 EVENT 状态
        self.threshold = 0                 # 当前阈值（手动修改后更新）
        self.setObjectName("zoneCard")
        self.setStyleSheet(
            "#zoneCard {background:#fafafa; border:1px solid #ccc;"
            "border-radius:8px;}"
            "#zoneCard:hover {border:2px solid #4a9eff;}"
            " QLabel {font-size:10pt;}")

        grid = QGridLayout(self)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setHorizontalSpacing(10)

        self.light = QLabel()
        self.light.setFixedSize(34, 34)

        self.name_label = QLabel(name)
        self.name_label.setStyleSheet("font-size:13pt; font-weight:bold;")
        self.mac_label = QLabel(mac)
        self.mac_label.setStyleSheet("color:#777; font-size:8pt;")

        self.score_label = QLabel("score -")
        self.last_event_label = QLabel("最近事件 -")
        self.feat_label = QLabel("var -  p2p -")
        self.rssi_label = QLabel("rssi -")
        self.calib_label = QLabel("未标定")
        self.calib_label.setStyleSheet("color:#555; font-size:9pt;")
        self.calib_btn = QPushButton("标定 15s")
        self.calib_btn.setToolTip("15s 倒计时（留人离场）后自动发 train，等 0x30 回填")
        self.calib_btn.clicked.connect(self._on_calib_clicked)

        grid.addWidget(self.light, 0, 0, 3, 1)
        grid.addWidget(self.name_label, 0, 1)
        grid.addWidget(self.mac_label, 1, 1)
        grid.addWidget(self.score_label, 0, 2)
        grid.addWidget(self.rssi_label, 1, 2)
        grid.addWidget(self.last_event_label, 2, 1)
        grid.addWidget(self.feat_label, 2, 2)
        grid.addWidget(self.calib_label, 3, 0, 1, 2)
        grid.addWidget(self.calib_btn, 3, 2)
        grid.setColumnStretch(1, 1)
        self.set_stale(True)

        # 卡片点击事件（用于选择卡片进行阈值调节）
        self.mousePressEvent = self._on_click  # 覆盖原生方法

    # ---- 显示更新 ----------------------------------------------------------

    def set_light(self, state, stale: bool = False):
        """根据状态设置指示灯颜色（中文：空闲=灰，运动=红，存在=黄）。"""
        if stale:
            color = "#d9d9d9"
        else:
            color = STATE_COLORS_CN.get(state, "#9e9e9e")
        self.light.setStyleSheet(
            "background:%s; border-radius:17px; border:2px solid #666;" % color)

    def set_stale(self, stale: bool):
        self.set_light(self._state, stale=stale)

    def update_record(self, rec: Record):
        if rec.kind == "EVENT":
            self._state = rec.state
            self.set_stale(False)
            self.score_label.setText("score %s" % rec.score)
            self.rssi_label.setText("rssi %d" % (rec.rssi if rec.rssi is not None else 0))
            # 中文状态名
            state_cn = STATE_NAMES_CN.get(rec.state, str(rec.state))
            hhmmss = datetime.fromtimestamp(rec.ts).strftime("%H:%M:%S")
            self.last_event_label.setText("最近 %s @%s" % (state_cn, hhmmss))
        elif rec.kind == "FEATURES":
            self.set_stale(False)
            self.feat_label.setText("var %d  p2p %d" % (rec.var or 0, rec.p2p or 0))
            self.rssi_label.setText("rssi %d" % (rec.rssi if rec.rssi is not None else 0))
        elif rec.kind == "CALIB":
            self.set_calib_result(rec)

    def set_name(self, name: str):
        """更新设备名称（从 MAC 映射或用户指定）。"""
        self.zone_name = name
        self.name_label.setText(name)

    @staticmethod
    def device_name_from_mac(mac: str) -> str:
        """根据 MAC 地址返回设备名称（如 1a:00:00:00:00:00 → 经典#1 TX1）。"""
        if mac:
            mac_clean = mac.replace(":", "").lower()
            last_byte = mac_clean[-2:]
            if last_byte in MAC_DEVICE_MAP:
                return MAC_DEVICE_MAP[last_byte][1]
        return ""

    def set_calib_result(self, rec: Record):
        if rec.raw is not None:
            self.calib_label.setText("阈值 %d / 噪声底 %d（train %ds）"
                                     % (rec.raw.threshold, rec.raw.noise_floor,
                                        rec.raw.train_duration))
        else:
            self.calib_label.setText(rec.text)
        self.calib_label.setStyleSheet("color:#1a7f37; font-size:9pt;")

    def set_threshold(self, threshold: int):
        """设置阈值（用户手动修改后更新显示）。"""
        self.threshold = threshold
        # 更新标定标签
        self.calib_label.setText("阈值 %d / 噪声底 - (手动调整)" % threshold)
        self.calib_label.setStyleSheet("color:#d62728; font-size:9pt;")  # 红色表示手动修改

    def set_calib_text(self, text: str, busy: bool = False):
        self.calib_label.setText(text)
        self.calib_label.setStyleSheet(
            "color:#b35900; font-size:9pt;" if busy else "color:#555; font-size:9pt;")

    def set_calib_enabled(self, enabled: bool):
        self.calib_btn.setEnabled(enabled)

    def _on_calib_clicked(self):
        self.calib_requested.emit(self.mac)

    def _on_click(self, event):
        """卡片点击事件（用于选择卡片进行阈值调节）。"""
        self.card_clicked.emit(self.mac)
        # 更新边框样式（视觉上高亮选中的卡片）
        self.setStyleSheet(
            "#zoneCard {background:#eef6ff; border:2px solid #4a9eff;"
            "border-radius:8px;} QLabel {font-size:10pt;}")
        super().mousePressEvent(event)  # 调用父类方法


# ===========================================================================
# 区域俯视图（粗粒度定位）
# ===========================================================================

# 设备锚点（归一化坐标：x 0=西→1=东，y 0=北→1=南；与实际方位对应：
# 经典#2 西北左上 / C3 东北右上 / S3 RX 西南左下 / 经典#1 东南右下）
ZONE_ANCHORS = {
    "00": (0.85, 0.85),      # 经典#1 TX1 → 东南（右下）
    "01": (0.85, 0.15),      # C3 TX2     → 东北（右上）
    "02": (0.15, 0.15),      # 经典#2 TX3 → 西北（左上）
}
RX_ANCHOR = (0.15, 0.85)     # S3 RX → 西南（左下）
ZONE_ACTIVE_MIN = 0.8        # 三链路归一化总响应低于此值 → 判定静止
ZONE_HOLD_S = 30.0           # 静止后保持"上次活动位置"的时长


class ZoneMapWidget(QWidget):
    """俯视区域图：四角画设备（三 TX + RX），按三链路 var 归一化响应
    加权显示人的估计位置。静止时保持"上次活动位置"并标注，超时淡出。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(316, 330)
        self._responses: dict[str, float] = {}   # mac → 归一化响应 r
        self._names: dict[str, str] = {}
        self._pos: tuple | None = None           # 估计位置 (x,y) 0..1
        self._total = 0.0                        # 总响应
        self._last_active_ts = 0.0               # 最近一次活动时刻
        self._hold_pos: tuple | None = None      # 静止保持位置

    # ---- 数据接口（Dashboard 每 500ms 调一次） ------------------------------

    def update_activity(self, responses: dict[str, float],
                        names: dict[str, str]):
        self._responses = dict(responses)
        self._names = dict(names)
        total = sum(v for v in self._responses.values() if v > 0)
        self._total = total
        now = time.time()
        if total >= ZONE_ACTIVE_MIN and self._responses:
            wx = wy = 0.0
            for mac, r in self._responses.items():
                if r <= 0:
                    continue
                ax, ay = ZONE_ANCHORS.get(mac, (0.5, 0.5))
                wx += ax * r
                wy += ay * r
            self._pos = (wx / total, wy / total)
            self._hold_pos = self._pos
            self._last_active_ts = now
        elif self._hold_pos is not None and (now - self._last_active_ts) > ZONE_HOLD_S:
            self._hold_pos = None                      # 超时淡出
            self._pos = None
        elif self._hold_pos is not None:
            self._pos = self._hold_pos                 # 静止：保持上次位置
        self.update()                                  # 触发 repaint

    # ---- 绘制 ---------------------------------------------------------------

    def _region_name(self, x: float, y: float) -> str:
        if 0.35 < x < 0.65 and 0.35 < y < 0.65:
            return "中心区"
        ns = "北" if y < 0.5 else "南"
        ew = "西" if x < 0.5 else "东"
        return ns + ew

    def paintEvent(self, ev):  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#fafafa"))
        p.setPen(QPen(QColor("#cccccc"), 1))
        p.drawRect(0, 0, w - 1, h - 1)
        p.setPen(QColor("#888888"))
        p.setFont(QFont("Microsoft YaHei", 8))
        p.drawText(6, 14, "区域俯视图（上=北 左=西）")

        m = 34                                    # 图形区边距
        gw, gh = w - 2 * m, h - 2 * m - 18
        gx, gy = m, m + 14

        def to_px(pt):
            return gx + pt[0] * gw, gy + pt[1] * gh

        # 链路虚线 TX→RX
        p.setPen(QPen(QColor("#bbbbbb"), 1, Qt.PenStyle.DashLine))
        for mac, axy in ZONE_ANCHORS.items():
            p.drawLine(*to_px(axy), *to_px(RX_ANCHOR))

        # 设备标记
        p.setFont(QFont("Microsoft YaHei", 8, QFont.Weight.Bold))
        for mac, axy in ZONE_ANCHORS.items():
            x, y = to_px(axy)
            p.setBrush(QBrush(QColor("#4a76d0")))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRect(x - 6, y - 6, 12, 12)
            name = self._names.get(mac) or MAC_DEVICE_MAP.get(mac, ("?", "?"))[1]
            p.setPen(QColor("#333333"))
            p.drawText(x - 40, y - 12, 80, 14,
                       Qt.AlignmentFlag.AlignCenter, name)
        rx_x, rx_y = to_px(RX_ANCHOR)
        p.setBrush(QBrush(QColor("#1a7f37")))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(rx_x - 7, rx_y - 7, 14, 14)
        p.setPen(QColor("#333333"))
        p.drawText(rx_x - 40, rx_y + 10, 80, 14,
                   Qt.AlignmentFlag.AlignCenter, "S3 RX")

        # 人的估计位置
        if self._pos is not None:
            x, y = to_px(self._pos)
            stale = (time.time() - self._last_active_ts) > 1.5   # 无实时活动
            body = QColor("#d62728") if not stale else QColor("#aaaaaa")
            p.setBrush(QBrush(body))
            p.setPen(Qt.PenStyle.NoPen)
            radius = 7 + min(6.0, self._total)
            p.drawEllipse(int(x - radius), int(y - radius),
                          int(radius * 2), int(radius * 2))
            p.setPen(QColor("#333333"))
            p.setFont(QFont("Microsoft YaHei", 8))
            label = ("上次活动 " if stale else "当前 ") + \
                self._region_name(*self._pos)
            p.drawText(6, h - 22, label)
            p.drawText(6, h - 8,
                       "活动度 %.1f" % self._total if not stale
                       else "静止/无活动（保持上次位置）")
        else:
            p.setPen(QColor("#999999"))
            p.setFont(QFont("Microsoft YaHei", 8))
            p.drawText(6, h - 22, "无人/未定位（等待活动…）")
        p.end()


# ===========================================================================
# 主窗口
# ===========================================================================

class Dashboard(QMainWindow):
    """组装顶栏/左列卡片/中央曲线/底部事件表+命令框，驱动数据管线。"""

    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        cfg = config or argparse.Namespace(
            source="serial", port="COM3", baud=2_000_000,
            udp_host="0.0.0.0", udp_port=3333,
            replay_file=None, replay_speed=4.0,
            data_root=None, auto_record=False, auto_start=False, title=None)
        self.cfg = cfg
        self.setWindowTitle(cfg.title or "ESP32 CSI 区域监控 · M3")
        self.resize(1320, 820)
        self.setMaximumSize(1920, 1200)  # 限制窗口最大尺寸

        self.source = None                 # 当前数据源（鸭子类型）
        self._command_channel = None       # send_line 或 None
        self.store = Store(data_root=cfg.data_root)
        self._record_queue: "queue.Queue[Record]" = queue.Queue()   # 回放入口
        self._names: dict[str, str] = {}   # mac → 区域名
        self._provisional: set = set()     # 缺 name_id 的临时名，等 EVENT 修正
        self._cards: dict[str, ZoneCard] = {}
        self._series: dict[str, dict] = {} # mac → {"t","var" deque}
        self._curve_items: dict = {}
        self._event_lines: list = []
        self._t0: float | None = None      # 曲线 x 轴原点（首个记录时刻）
        self._last_seen: dict[str, float] = {}
        self._hb = None
        self._frames_since_fps = 0
        self._bad_payload = 0
        self._device_info: ProbeResult | None = None   # 启动扫描识别到的设备
        self._device_shown = False                     # 设备信息是否已显示
        self._discovered_macs: set = set()             # 已发现的 MAC 集合（避免重复日志）
        self._selected_card: ZoneCard | None = None    # 当前选中的卡片（用于阈值调节）
        self._noise_floor: dict[str, float] = {}       # mac → 标定噪声底（CALIB 0x30 回填）
        self._var_hist: dict[str, deque] = {}          # mac → var 均值历史（120s，未标定时滚动最小值当噪声底代理）
        self.frozen = False                            # 冻结状态（暂停 UI 更新）
        self._calib_test_running = False               # 全局标定测试进行中
        self._calib_test_progress = {}                 # {"mac": "counting"/"waiting"/"done"/"timeout"}
        self._calib_test_countdown = 0                 # 全局标定倒计时计数器
        self.auto_y = True                             # Y 轴自动/手动量程
        self.y_min = 0                                 # 手动量程下限
        self.y_max = 1000                              # 手动量程上限（默认 1000centi，覆盖大部分场景）

        self._build_ui()
        self._calib = None                 # {"mac","count","timer","timeout"}

        # 三个节拍器：排空队列 50ms / 曲线刷新 200ms / 统计与卡片新鲜度 500ms
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(50)
        self._poll_timer.timeout.connect(self._poll)
        self._plot_timer = QTimer(self)
        self._plot_timer.setInterval(200)
        self._plot_timer.timeout.connect(self._refresh_plot)
        self._stat_timer = QTimer(self)
        self._stat_timer.setInterval(500)
        self._stat_timer.timeout.connect(self._refresh_stats)
        for t in (self._poll_timer, self._plot_timer, self._stat_timer):
            t.start()

        if cfg.auto_start:
            self._start_source()

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        central = QWidget()
        root = QHBoxLayout(central)

        # 左列：区域卡片
        left = QVBoxLayout()
        head = QLabel("监测区域")
        head.setStyleSheet("font-size:12pt; font-weight:bold;")
        left.addWidget(head)
        self.cards_area = QScrollArea()
        self.cards_area.setWidgetResizable(True)
        cards_holder = QWidget()
        self.cards_layout = QVBoxLayout(cards_holder)
        self.cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.cards_layout.setSpacing(8)
        self.cards_area.setWidget(cards_holder)
        self.cards_area.setFixedWidth(330)
        left.addWidget(self.cards_area, 1)
        self.cards_hint = QLabel("等待数据…\n（出现 peer 后自动生成卡片）")
        self.cards_hint.setStyleSheet("color:#888;")
        left.addWidget(self.cards_hint)
        # 区域俯视图（粗粒度定位：三链路归一化响应加权）
        self.zone_map = ZoneMapWidget()
        left.addWidget(self.zone_map, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addLayout(left, 1)  # 左列吸收多余空间，右列跟随内容宽度

        # 右侧：顶栏 + 曲线 + 事件表/命令框
        right = QVBoxLayout()

        # ---- 顶栏拆成两行，避免窗口被撑宽 ----

        # 第一行：核心操作（数据源/连接/记录/复位/冻结）
        top_row1 = QHBoxLayout()
        top_row1.addWidget(QLabel("数据源"))
        self.source_combo = QComboBox()
        self.source_combo.addItems(["串口", "UDP 监听", "回放 CSV", "模拟数据"])
        self.source_combo.setCurrentIndex(
            {"serial": 0, "udp": 1, "replay": 2, "sim": 3}.get(self.cfg.source, 0))
        top_row1.addWidget(self.source_combo)
        self.param_edit = QLineEdit(self.cfg.port if self.cfg.source == "serial"
                                    else str(self.cfg.udp_port))
        self.param_edit.setFixedWidth(110)
        self.param_edit.setToolTip("串口模式填 COM 口；UDP 模式填监听端口")
        top_row1.addWidget(self.param_edit)
        self.connect_btn = QPushButton("连接")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        top_row1.addWidget(self.connect_btn)
        self.record_btn = QPushButton("开始记录")
        self.record_btn.clicked.connect(self._on_record_clicked)
        top_row1.addWidget(self.record_btn)
        self.rec_label = QLabel("○ 未记录")
        self.rec_label.setStyleSheet("color:#888;")
        top_row1.addWidget(self.rec_label)
        self.reset_btn = QPushButton("复位")
        self.reset_btn.setStyleSheet("color:#d62728;")
        self.reset_btn.clicked.connect(self._on_reset_clicked)
        top_row1.addWidget(self.reset_btn)
        self.freeze_btn = QPushButton("冻结")
        self.freeze_btn.setStyleSheet("color:#2e7d32;")
        self.freeze_btn.setCheckable(True)
        self.freeze_btn.setChecked(False)
        self.freeze_btn.toggled.connect(self._on_toggle_freeze)
        top_row1.addWidget(self.freeze_btn)
        self.freeze_label = QLabel("")
        self.freeze_label.setStyleSheet("color:#2e7d32; font-weight:bold;")
        top_row1.addWidget(self.freeze_label)
        right.addLayout(top_row1)

        # 第二行：辅助功能（量程/阈值/标定/统计）
        top_row2 = QHBoxLayout()
        self.auto_y_check = QCheckBox("自动量程")
        self.auto_y_check.setChecked(True)
        self.auto_y_check.stateChanged.connect(self._on_auto_y_changed)
        top_row2.addWidget(self.auto_y_check)

        # 阈值调节
        threshold_row = QHBoxLayout()
        threshold_row.addWidget(QLabel("阈值："))
        self.threshold_mac_label = QLabel("(选择卡片)")
        self.threshold_mac_label.setStyleSheet("color:#555; font-size:9pt;")
        threshold_row.addWidget(self.threshold_mac_label)
        self.threshold_value_edit = QLineEdit()
        self.threshold_value_edit.setPlaceholderText("输入新阈值")
        self.threshold_value_edit.setFixedWidth(100)
        threshold_row.addWidget(self.threshold_value_edit)
        self.threshold_apply_btn = QPushButton("应用")
        self.threshold_apply_btn.clicked.connect(self._on_threshold_apply)
        threshold_row.addWidget(self.threshold_apply_btn)
        top_row2.addLayout(threshold_row)

        # 全量标定测试
        self.global_calib_btn = QPushButton("全量标定")
        self.global_calib_btn.setStyleSheet(
            "background:#fff7e6; color:#d46b08; border:1px solid #d46b08;")
        self.global_calib_btn.clicked.connect(self._on_global_calib_test)
        self.global_calib_btn.setEnabled(False)
        top_row2.addWidget(self.global_calib_btn)
        self.global_calib_label = QLabel("")
        self.global_calib_label.setStyleSheet("color:#d46b08; font-weight:bold; font-size:9pt;")
        top_row2.addWidget(self.global_calib_label)
        top_row2.addStretch(1)

        # 统计信息（右对齐）
        self.stat_label = QLabel("帧率 - | 丢帧 -")
        self.stat_label.setStyleSheet("font-family:Consolas,monospace; font-size:9pt;")
        top_row2.addWidget(self.stat_label)
        self.hb_label = QLabel("心跳 -")
        self.hb_label.setStyleSheet("font-family:Consolas,monospace; font-size:9pt;")
        top_row2.addWidget(self.hb_label)
        right.addLayout(top_row2)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.plot = pg.PlotWidget(title="每链路 var 特征（竖线=事件，颜色=状态）")
        self.plot.addLegend(offset=(8, 8))
        self.plot.setLabel("bottom", "运行秒")
        self.plot.setLabel("left", "var (centi)")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.enableAutoRange(x=False, y=True)
        self.plot.setXRange(0, 60, padding=0)
        splitter.addWidget(self.plot)

        bottom = QWidget()
        bottom.setLayout(QVBoxLayout(bottom))
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        # 限制 splitter 横向扩展（内部子控件默认 Expanding 会撑满）
        splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        splitter.setMaximumWidth(1800)
        right.addWidget(splitter, 0)
        root.addLayout(right, 0)

        # 限制窗口最大尺寸，防止越拖越宽
        self.setMaximumSize(1920, 1200)
        self.setCentralWidget(central)
        self._build_bottom(bottom)

    def _build_bottom(self, bottom: QWidget):
        bv = bottom.layout()
        self.event_table = QTableWidget(0, 6)
        self.event_table.setHorizontalHeaderLabels(
            ["时间", "区域", "MAC", "事件", "score", "RSSI"])
        self.event_table.verticalHeader().setVisible(False)
        self.event_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, w in enumerate((90, 90, 135, 90, 60, 60)):
            self.event_table.setColumnWidth(i, w)
        self.event_table.horizontalHeader().setStretchLastSection(True)
        bv.addWidget(self.event_table, 3)

        cmd_row = QHBoxLayout()
        self.cmd_edit = QLineEdit()
        self.cmd_edit.setPlaceholderText(
            "REPL 命令（回车发送）：peer list / train <mac> 15 / threshold save …")
        self.cmd_edit.returnPressed.connect(self._send_command)
        self.cmd_btn = QPushButton("发送")
        self.cmd_btn.clicked.connect(self._send_command)
        cmd_row.addWidget(QLabel("命令"))
        cmd_row.addWidget(self.cmd_edit, 1)
        cmd_row.addWidget(self.cmd_btn)
        bv.addLayout(cmd_row)

        self.cmd_log = QPlainTextEdit()
        self.cmd_log.setReadOnly(True)
        self.cmd_log.setMaximumBlockCount(1000)
        self.cmd_log.setPlaceholderText("命令回显 / 0x20 日志 / 标定结果…")
        self.cmd_log.setMinimumHeight(70)
        bv.addWidget(self.cmd_log, 1)

    # ------------------------------------------------------------ 数据源 ----

    def _on_connect_clicked(self):
        if self.source is not None:
            self._stop_source()
        else:
            self._start_source()

    def _start_source(self) -> bool:
        kind = ("serial", "udp", "replay", "sim")[self.source_combo.currentIndex()]
        param = self.param_edit.text().strip()
        try:
            if kind == "serial":
                src = SerialSource(port=param or "COM3", baud=self.cfg.baud)
                src.start()
            elif kind == "udp":
                src = UdpSource(host=self.cfg.udp_host, port=int(param or 3333))
                src.start()
            elif kind == "sim":
                src = SimSource()
                src.start()
            elif kind == "replay":
                path = self.cfg.replay_file or QFileDialog.getOpenFileName(
                    self, "选择回放 CSV", str(self.store.data_root),
                    "CIS CSV (cis_log.csv *.csv)")[0]
                if not path:
                    return False
                src = _ReplayAdapter(path, speed=self.cfg.replay_speed,
                                     record_queue=self._record_queue)
                src.start()
            else:
                raise ValueError("unknown source: %s" % kind)
        except Exception as exc:
            QMessageBox.critical(self, "连接失败", "%s" % exc)
            return False
        self.source = src
        self._command_channel = getattr(src, "send_line", None)
        self._t0 = None
        self.connect_btn.setText("断开")
        cmd_ok = self._command_channel is not None
        for card in self._cards.values():
            card.set_calib_enabled(cmd_ok)
        self.cmd_edit.setEnabled(cmd_ok)
        self.cmd_btn.setEnabled(cmd_ok)
        if not cmd_ok:
            self.log("当前数据源无命令通道（REPL 走串口），命令/标定禁用")
        if self.cfg.auto_record and not self.store.is_recording:
            self._start_record()
        self.log("数据源已连接：%s" % src_desc(src))
        # 查询 peer list 并设置设备映射（自动识别设备名称）
        if cmd_ok:
            self._query_peers_and_map()
            # 连接成功后启用全局标定按钮
            self.global_calib_btn.setEnabled(True)
        return True

    def _stop_source(self):
        if self.source is None:
            return
        try:
            self.source.stop()
        except Exception:
            pass
        self.log("数据源已断开")
        self.source = None
        self._command_channel = None
        self.connect_btn.setText("连接")
        for card in self._cards.values():
            card.set_calib_enabled(False)
        self.cmd_edit.setEnabled(False)
        self.cmd_btn.setEnabled(False)
        if self._calib:
            self._cancel_calib(self._calib["mac"])
            self._reset_calib_button()
        # 断开时重置全局标定状态
        if self._calib_test_running:
            self._reset_global_calib_state()
            for card in self._cards.values():
                card.set_calib_enabled(False)

    # ------------------------------------------------------------ 记录 ------

    def _on_record_clicked(self):
        if self.store.is_recording:
            self.store.stop_session()
            self.log("记录已停止（%d 行）" % self.store.rows_written)
        else:
            self._start_record()

    def _start_record(self):
        path = self.store.start_session()
        self.rec_label.setStyleSheet("color:#d62728; font-weight:bold;")
        self.log("开始记录 → %s" % path)

    # ------------------------------------------------------------ 管线 ------

    def _poll(self):
        """排空 frame_queue（帧源）与 _record_queue（回放），统一分发。"""
        src = self.source
        if src is None:
            return
        fq = getattr(src, "frame_queue", None)
        if fq is not None:
            for _ in range(600):
                try:
                    frame = fq.get_nowait()
                except queue.Empty:
                    break
                rec = frame_to_record(frame, name_lookup=self._name_for)
                if rec is None:
                    self._bad_payload += 1
                    continue
                # 冻结时不更新 UI，但继续从队列取数据（避免队列满）
                if self.frozen:
                    continue
                self._dispatch(rec)
        while True:
            try:
                rec = self._record_queue.get_nowait()
            except queue.Empty:
                break
            # 冻结时不更新 UI
            if self.frozen:
                continue
            self._dispatch(rec)

    def _on_toggle_freeze(self):
        """切换冻结状态。"""
        self.frozen = self.freeze_btn.isChecked()
        if self.frozen:
            self.freeze_btn.setText("解冻")
            self.freeze_label.setText("⏸ 已冻结（数据继续流入，UI 暂停更新）")
            self.log("[冻结] 已冻结 UI 更新（数据继续流入，卡片/曲线/事件表暂停更新）")
            # 冻结时停止 CSV 记录
            if self.store.is_recording:
                self.store.stop_session()
                self.rec_label.setText("○ 已冻结（记录暂停）")
        else:
            self.freeze_btn.setText("冻结")
            self.freeze_label.setText("")
            self.log("[解冻] 恢复 UI 更新")
            # 解冻时恢复 CSV 记录（如果之前是记录状态）
            if not self.store.is_recording and self.cfg.auto_record:
                self._start_record()

    # ---- 命名与卡片 ---------------------------------------------------------

    def _name_for(self, mac: str, name_id: int = 0) -> str:
        """mac → 设备名。优先使用 MAC 映射（经典#1 TX1 等），其次用区域名。"""
        if mac not in self._names:
            # 先尝试 MAC 映射
            device_name = ZoneCard.device_name_from_mac(mac)
            if device_name:
                self._names[mac] = device_name
                return device_name
            # 然后用 name_id 或默认名
            if name_id:
                self._names[mac] = "区域%d" % name_id
            else:
                self._names[mac] = default_zone_name(mac)
                self._provisional.add(mac)
        elif name_id and mac in self._provisional:
            self._provisional.discard(mac)
            self._names[mac] = "区域%d" % name_id
            card = self._cards.get(mac)
            if card is not None:
                card.set_name(self._names[mac])
        return self._names[mac]

    def set_device_mapping(self, peers: list[tuple[str, str]]):
        """设置 peer 映射（MAC, name）→ 卡片设备名。

        调用示例：
            self.set_device_mapping([("1a:00:00:00:00:00", "经典#1 TX1"),
                                    ("1a:00:00:00:00:01", "C3 TX2"),
                                    ("1a:00:00:00:00:02", "经典#2 TX3")])
        """
        for mac, name in peers:
            self._names[mac] = name
            # 更新现有卡片
            if mac in self._cards:
                self._cards[mac].set_name(name)

    def _get_card(self, mac: str, name_id: int = 0) -> ZoneCard:
        card = self._cards.get(mac)
        if card is None:
            card = ZoneCard(mac, self._name_for(mac, name_id))
            card.calib_requested.connect(self._start_calib)
            card.card_clicked.connect(self._on_card_clicked)  # 连接卡片点击事件
            self.cards_layout.addWidget(card)
            self.cards_hint.hide()
            self._cards[mac] = card
            card.set_calib_enabled(self._command_channel is not None)
        return card

    def _on_card_clicked(self, mac: str):
        """卡片被点击时选中（用于阈值调节）。"""
        self._selected_card = self._cards.get(mac)
        if self._selected_card:
            self.threshold_mac_label.setText(self._selected_card.zone_name + " " + mac)
            # 更新阈值输入框显示当前阈值
            self.threshold_value_edit.setText(str(self._selected_card.threshold))
            self.log("[选择] 当前卡片：%s (%s)" % (self._selected_card.zone_name, mac))
        else:
            self.threshold_mac_label.setText("(未选择)")

    def _dispatch(self, rec: Record):
        if self._t0 is None:
            self._t0 = rec.ts
        self._frames_since_fps += 1
        if rec.mac:
            self._last_seen[rec.mac] = rec.ts
            card = self._get_card(rec.mac)
            card.update_record(rec)
        if self.store.is_recording:
            self.store.record(rec)

        kind = rec.kind
        if kind == "HEARTBEAT":
            self._hb = rec.raw
            # 首个心跳 → 显示设备自检信息
            self._show_device_info()
        elif kind == "EVENT":
            # 新 MAC 发现 → 记录链路信息
            if rec.mac and rec.mac not in self._discovered_macs:
                self._discovered_macs.add(rec.mac)
                self.log("[链路] 发现新 MAC: %s (区域 %s)" % (rec.mac.upper(),
                                                             rec.name or rec.mac))
            self._add_event_line(rec)
            self._add_event_row(rec)
        elif kind == "FEATURES":
            s = self._series.setdefault(
                rec.mac, {"t": deque(maxlen=CURVE_MAX_POINTS),
                          "var": deque(maxlen=CURVE_MAX_POINTS)})
            s["t"].append(rec.ts)
            s["var"].append(rec.var or 0)
        elif kind == "CALIB":
            self._add_event_row(rec)
            self.log("CALIB %s %s" % (rec.mac, rec.text))
            # 回填噪声底（区域俯视图归一化响应用）
            if rec.raw is not None and getattr(rec.raw, "noise_floor", None):
                self._noise_floor[rec.mac] = float(rec.raw.noise_floor)
            if self._calib and self._calib["mac"] == rec.mac:
                self._finish_calib(rec)
                # 全局标定测试收到 0x30 结果时也更新
                if self._calib_test_running:
                    self._global_calib_on_result(rec.mac)
        elif kind == "LOG":
            self.log("RX: %s" % rec.text)

    # ------------------------------------------------------------ 曲线 ------

    def _add_event_line(self, rec: Record):
        x = rec.ts - (self._t0 or rec.ts)
        line = pg.InfiniteLine(
            pos=x, angle=90, movable=False,
            pen=pg.mkPen(QColor(STATE_COLORS.get(rec.state, "#9e9e9e")),
                         width=1,
                         style=EVENT_LINE_STYLES.get(rec.state,
                                                     Qt.PenStyle.SolidLine)))
        self.plot.addItem(line)
        self._event_lines.append(line)
        while len(self._event_lines) > EVENT_LINE_MAX:
            self.plot.removeItem(self._event_lines.pop(0))

    def _refresh_plot(self):
        for mac, s in self._series.items():
            item = self._curve_items.get(mac)
            if item is None:
                color = PEER_COLORS[len(self._curve_items) % len(PEER_COLORS)]
                item = self.plot.plot(pen=pg.mkPen(QColor(color), width=1),
                                      name=self._names.get(mac, mac))
                self._curve_items[mac] = item
            if s["t"]:
                t0 = self._t0 or s["t"][0]
                item.setData([t - t0 for t in s["t"]], list(s["var"]))
        if self._series and self._t0 is not None:
            x_now = time.time() - self._t0
            self.plot.setXRange(max(0.0, x_now - 120.0), x_now + 2.0, padding=0)

    # ------------------------------------------------------------ 事件表 ----

    def _add_event_row(self, rec: Record):
        tbl = self.event_table
        tbl.insertRow(0)
        hhmmss = datetime.fromtimestamp(rec.ts).strftime("%H:%M:%S")
        if rec.kind == "EVENT":
            ev_text, color = rec.state_name(), STATE_COLORS.get(rec.state)
        else:                                   # CALIB
            ev_text, color = "标定完成", "#1a7f37"
        cells = (hhmmss, rec.name or "-", rec.mac or "-", ev_text,
                 "" if rec.score is None else rec.score,
                 "" if rec.rssi is None else rec.rssi)
        for col, val in enumerate(cells):
            it = QTableWidgetItem(str(val))
            if col == 3 and color:
                it.setForeground(QBrush(QColor(color)))
            tbl.setItem(0, col, it)
        while tbl.rowCount() > TABLE_MAX_ROWS:
            tbl.removeRow(tbl.rowCount() - 1)

    def _on_reset_clicked(self):
        """复位所有事件和曲线（清除数据，保留设备映射和配置）。"""
        # 清除事件表
        self.event_table.setRowCount(0)
        # 清除曲线上的事件竖线
        for line in self._event_lines:
            self.plot.removeItem(line)
        self._event_lines.clear()
        # 清除曲线数据
        for mac in self._series:
            self._series[mac]["t"].clear()
            self._series[mac]["var"].clear()
        for item in self._curve_items.values():
            self.plot.removeItem(item)
        self._curve_items.clear()
        # 重置卡片状态
        for card in self._cards.values():
            card.set_stale(True)
            card.last_event_label.setText("最近事件 -")
            card.score_label.setText("score -")
            card.feat_label.setText("var -  p2p -")
            card.rssi_label.setText("rssi -")
        # 重置曲线 x 轴和 y 轴
        self._t0 = None
        self.plot.setXRange(0, 60, padding=0)
        self._apply_y_range()
        self.log("[复位] 已清除所有事件和曲线数据")

    def _on_auto_y_changed(self, state: int):
        """Y 轴自动/手动量程切换（state: 0=未勾选手动，2=已勾选自动）。"""
        self.auto_y = (state == Qt.CheckState.Checked)
        self._apply_y_range()
        if not self.auto_y:
            self.log("[量程] 切换为手动 Y 轴：[%d, %d]" % (self.y_min, self.y_max))
        else:
            self.log("[量程] 切换为自动 Y 轴")

    def _apply_y_range(self):
        """应用当前 Y 轴量程设置（自动或手动）。"""
        if self.auto_y:
            self.plot.enableAutoRange(y=True)
        else:
            self.plot.setYRange(self.y_min, self.y_max, padding=0.1)

    def set_y_range(self, y_min: float, y_max: float):
        """手动设置 Y 轴量程。"""
        self.y_min = y_min
        self.y_max = y_max
        self.auto_y_check.setChecked(False)
        self._apply_y_range()

    # ------------------------------------------------------------ 标定 ------

    def _start_calib(self, mac: str):
        card = self._cards.get(mac)
        if card is None:
            return
        if self._command_channel is None:
            self.log("标定失败：当前数据源无命令通道（REPL 走串口）")
            return
        if self._calib is not None:
            self.log("标定失败：已有进行中的标定（%s）" % self._calib["mac"])
            return
        if self._calib_test_running:
            self.log("标定失败：全局标定测试进行中")
            return
        self._calib = {"mac": mac, "count": CALIB_COUNTDOWN_S,
                       "timer": None, "timeout": None}
        card.calib_btn.setEnabled(False)
        card.set_calib_text("清场倒计时 %ds…" % CALIB_COUNTDOWN_S, busy=True)
        self.log("标定[%s]：%ds 倒计时，请清场离场" % (mac, CALIB_COUNTDOWN_S))
        timer = QTimer(self)
        timer.setInterval(1000)
        timer.timeout.connect(self._calib_tick)
        timer.start()
        self._calib["timer"] = timer

    def _calib_tick(self):
        if not self._calib:
            return
        self._calib["count"] -= 1
        card = self._cards.get(self._calib["mac"])
        if self._calib["count"] > 0:
            if card:
                card.set_calib_text("清场倒计时 %ds…" % self._calib["count"], busy=True)
            return
        self._calib["timer"].stop()
        self._calib["timer"].deleteLater()
        self._calib["timer"] = None
        ok = False
        if self._command_channel is not None:
            ok = self._command_channel(
                "train %s %d" % (self._calib["mac"], CALIB_COUNTDOWN_S))
        if ok:
            if card:
                card.set_calib_text("等待 0x30 标定结果…", busy=True)
            timeout = QTimer(self)
            timeout.setSingleShot(True)
            timeout.setInterval(CALIB_WAIT_0X30_S * 1000)
            timeout.timeout.connect(self._calib_timeout)
            timeout.start()
            self._calib["timeout"] = timeout
            self.log("已发送 train %s %d" % (self._calib["mac"], CALIB_COUNTDOWN_S))
        else:
            self.log("标定[%s]中止：命令通道写入失败" % self._calib["mac"])
            if card:
                card.set_calib_text("train 命令发送失败", busy=False)
            self._reset_calib_button()
            self._calib = None

    def _calib_timeout(self):
        if not self._calib:
            return
        self.log("标定[%s]超时：%.0fs 内未收到 0x30"
                 % (self._calib["mac"], CALIB_WAIT_0X30_S))
        card = self._cards.get(self._calib["mac"])
        if card:
            card.set_calib_text("标定超时", busy=False)
        self._reset_calib_button()
        self._calib = None

    # ---- 全局标定测试 -------------------------------------------------------

    def _on_global_calib_test(self):
        """全量标定测试：倒计时后向所有 peer 发 train，等 0x30 结果。"""
        if self._calib_test_running:
            self.log("[标定测试] 已有进行中的标定测试")
            return
        if self._command_channel is None:
            self.log("[标定测试] 失败：无命令通道（需串口连接）")
            return
        all_macs = list(self._cards.keys())
        if not all_macs:
            self.log("[标定测试] 暂无已发现的 peer，请先运行一段时间再标定")
            return
        self._calib_test_running = True
        self._calib_test_countdown = CALIB_TEST_COUNTDOWN_S
        self.global_calib_btn.setEnabled(False)
        # 全局标定时禁用单卡标定
        for card in self._cards.values():
            card.calib_btn.setEnabled(False)
        self.global_calib_label.setText("⏱ 全量标定 %ds…" % CALIB_TEST_COUNTDOWN_S)
        self.log("[标定测试] %d 个 peer 准备，%ds 倒计时，请清场" % (len(all_macs), CALIB_TEST_COUNTDOWN_S))
        self._calib_test_progress = {mac: "counting" for mac in all_macs}
        timer = QTimer(self)
        timer.setInterval(1000)
        timer.timeout.connect(self._global_calib_tick)
        timer.start()
        self._calib_test_timer = timer

    def _global_calib_tick(self):
        self._calib_test_countdown -= 1
        count = self._calib_test_countdown
        self.global_calib_label.setText(
            "⏱ 全量标定 %ds…请清场离场" % count)
        if count <= 0:
            self._calib_test_timer.stop()
            self._calib_test_timer.deleteLater()
            self._calib_test_timer = None
            self._fire_train_all()
            return
        for card in self._cards.values():
            if self._calib_test_progress.get(card.mac) == "counting":
                card.set_calib_text("标定倒计时 %ds…" % count, busy=True)

    def _fire_train_all(self):
        all_done = True
        for mac in list(self._calib_test_progress.keys()):
            if self._calib_test_progress[mac] != "counting":
                continue
            self._calib_test_progress[mac] = "waiting"
            ok = self._command_channel("train %s %d" % (mac, CALIB_COUNTDOWN_S))
            if ok:
                card = self._cards.get(mac)
                if card:
                    card.set_calib_text("等待 0x30 标定结果…", busy=True)
                self.log("[标定测试] → train %s %ds" % (mac, CALIB_COUNTDOWN_S))
            else:
                self.log("[标定测试] ✗ train %s 发送失败" % mac)
                self._calib_test_progress[mac] = "fail"
                all_done = False
        if all_done:
            self._calib_test_timeout_timer = QTimer(self)
            self._calib_test_timeout_timer.setSingleShot(True)
            self._calib_test_timeout_timer.setInterval(CALIB_WAIT_0X30_S * 1000)
            self._calib_test_timeout_timer.timeout.connect(self._global_calib_timeout)
            self._calib_test_timeout_timer.start()
        else:
            self.global_calib_label.setText("部分 train 发送失败")
            self._calib_test_running = False
            self.global_calib_btn.setEnabled(True)

    def _global_calib_on_result(self, mac: str):
        """收到某个 peer 的 0x30 标定结果。"""
        if mac in self._calib_test_progress:
            self._calib_test_progress[mac] = "done"
            card = self._cards.get(mac)
            if card:
                card.set_calib_text("标定完成 ✓", busy=False)
            self.log("[标定测试] ✓ %s 标定结果已接收" % mac)
        remaining = [v for v in self._calib_test_progress.values()
                     if v in ("counting", "waiting")]
        if not remaining:
            self._global_calib_test_complete()

    def _global_calib_test_complete(self):
        done_count = sum(1 for v in self._calib_test_progress.values() if v == "done")
        fail_count = sum(1 for v in self._calib_test_progress.values() if v in ("timeout", "fail"))
        total = len(self._calib_test_progress)
        self.global_calib_label.setText("✓ 标定完成 (%d/%d)" % (done_count, total))
        self.log("[标定测试] 全部完成：%d OK, %d FAIL" % (done_count, fail_count))
        self._calib_test_running = False
        self.global_calib_btn.setEnabled(True)
        # 恢复单卡标定按钮
        for card in self._cards.values():
            card.calib_btn.setEnabled(self._command_channel is not None)
        t = getattr(self, "_calib_test_timeout_timer", None)
        if t is not None:
            try:
                t.stop()
                t.deleteLater()
            except:
                pass

    def _global_calib_timeout(self):
        for mac, status in self._calib_test_progress.items():
            if status == "waiting":
                self._calib_test_progress[mac] = "timeout"
                card = self._cards.get(mac)
                if card:
                    card.set_calib_text("标定超时", busy=False)
                self.log("[标定测试] ✗ %s 标定超时" % mac)
        self.global_calib_label.setText("⚠ 标定超时")
        self._global_calib_test_complete()

    def _reset_global_calib_state(self):
        self._calib_test_running = False
        self._calib_test_progress = {}
        self.global_calib_btn.setEnabled(True)
        self.global_calib_label.setText("")
        t = getattr(self, "_calib_test_timer", None)
        if t is not None:
            try:
                t.stop()
                t.deleteLater()
            except:
                pass
        t = getattr(self, "_calib_test_timeout_timer", None)
        if t is not None:
            try:
                t.stop()
                t.deleteLater()
            except:
                pass

    def _finish_calib(self, rec: Record):
        self._cancel_calib(rec.mac)
        self._reset_calib_button()

    def _cancel_calib(self, mac: str):
        if self._calib and self._calib["mac"] == mac:
            for key in ("timer", "timeout"):
                t = self._calib.get(key)
                if t is not None:
                    t.stop()
                    t.deleteLater()
            self._calib = None

    def _reset_calib_button(self):
        mac = self._calib["mac"] if self._calib else None
        for card in self._cards.values():
            if mac is None or card.mac == mac:
                card.calib_btn.setText("重新标定 %ds" % CALIB_COUNTDOWN_S)
                card.calib_btn.setEnabled(self._command_channel is not None)

    # ------------------------------------------------------------ 命令/日志 --

    def _send_command(self):
        text = self.cmd_edit.text().strip()
        if not text:
            return
        if self._command_channel is None:
            self.log(">> 命令通道不可用（当前数据源非串口）")
            return
        ok = self._command_channel(text)
        self.log(">> %s%s" % (text, "" if ok else "   [发送失败]"))
        self.cmd_edit.clear()

    def _on_threshold_apply(self):
        """应用阈值调节（用户输入新阈值，应用到当前选中的 peer）。"""
        new_threshold = self.threshold_value_edit.text().strip()
        if not new_threshold:
            self.log("[阈值] 请输入新阈值 (centi-var)")
            return

        # 获取当前选中的 MAC（从卡片或事件表）
        selected_mac = None
        if self._selected_card:
            selected_mac = self._selected_card.mac
        elif self.event_table.rowCount() > 0:
            # 从事件表最后一行获取 MAC
            last_row = self.event_table.rowCount() - 1
            mac_item = self.event_table.item(last_row, 2)  # MAC 列
            if mac_item:
                selected_mac = mac_item.text()

        if not selected_mac:
            self.log("[阈值] 请先选择卡片或从事件表选择一条记录")
            return

        try:
            new_val = int(new_threshold)
        except ValueError:
            self.log("[阈值] 阈值必须是整数 (centi-var)")
            return

        # 发送阈值修改命令
        command = "threshold %s %d" % (selected_mac, new_val)
        if self._command_channel:
            ok = self._command_channel(command)
            self.log("[%s] 阈值修改：阈值=%d c  %s" % (
                selected_mac, new_val, "成功" if ok else "失败"))
            # 更新卡片显示
            if self._selected_card and self._selected_card.mac == selected_mac:
                self._selected_card.set_threshold(new_val)
        else:
            self.log("[阈值] 命令通道不可用")

    def log(self, text: str):
        self.cmd_log.appendPlainText(
            "%s  %s" % (datetime.now().strftime("%H:%M:%S"), text))

    def _show_device_info(self, info: ProbeResult | None = None):
        """显示设备自检信息（启动扫描结果或连接后识别）。"""
        if self._device_shown:
            return
        self._device_shown = True

        if info is None:
            info = self._device_info
        if info is None:
            return

        self.log("[自检] === 设备身份 ===")
        self.log("[自检] 端口：%s  波特率：%d" % (info.port, info.baud))
        if info.banner:
            # 用 _clean_text 清理二进制帧数据
            clean = _clean_text(info.banner, max_len=120)
            if clean.strip():
                self.log("[自检] Banner: %s" % clean.strip())
        if info.mac:
            self.log("[自检] MAC: %s" % info.mac.upper())
        if info.uptime_ms is not None:
            self.log("[自检] 已运行：%.1f 秒" % (info.uptime_ms / 1000))
        if info.free_heap is not None:
            heap_mb = info.free_heap / (1024 * 1024)
            self.log("[自检] 剩余堆：%.2f MB" % heap_mb)
        if info.peer_count is not None:
            self.log("[自检] 已注册 Peer: %d" % info.peer_count)
        if info.commands:
            self.log("[自检] 可用命令：%s" % ", ".join(info.commands))
        self.log("[自检] ================")

    def _query_peers_and_map(self):
        """查询 RX peer list 并设置卡片设备名映射。"""
        if self.source is None or not hasattr(self.source, "send_line"):
            return
        try:
            from self_test import query_peer_list
            peer_result = query_peer_list(
                self.source.port, self.source.baud, timeout_s=2.0)
            peers = peer_result.get("peers", [])
            if peers:
                self.set_device_mapping(peers)
                self.log("[自检] 设备映射：")
                for mac, name in peers:
                    self.log("[自检]   %s → %s" % (mac, name))
        except Exception as exc:
            self.log("[自检] 查询 peer list 失败：%s" % exc)

    # ------------------------------------------------------------ 刷新 ------

    def _refresh_stats(self):
        now = time.time()
        self._fps = self._frames_since_fps * 2.0        # 500ms 窗口折算
        self._frames_since_fps = 0
        parts = ["帧率 %.1f/s" % self._fps]
        src_stats = {}
        if self.source is not None and hasattr(self.source, "stats"):
            try:
                src_stats = self.source.stats() or {}
            except Exception:
                src_stats = {}
        if src_stats:
            parts.append("ok %s" % src_stats.get("frames_ok", "-"))
            if src_stats.get("bad_crc"):
                parts.append("crc错 %s" % src_stats["bad_crc"])
            parts.append("队列丢 %s" % src_stats.get("queue_dropped", 0))
            if "device_dropped_frames" in src_stats:
                parts.append("固件丢 %s" % src_stats["device_dropped_frames"])
            if src_stats.get("bytes_dropped"):
                parts.append("环丢字节 %s" % src_stats["bytes_dropped"])
            if "rows_sent" in src_stats:
                parts.append("回放已发 %s" % src_stats["rows_sent"])
        if self._bad_payload:
            parts.append("坏payload %d" % self._bad_payload)
        self.stat_label.setText(" | ".join(parts))
        # 心跳 / 回放进度
        if isinstance(self._hb, protocol.Heartbeat):
            self.hb_label.setText(
                "心跳 uptime %.0fs | peers %d | heap %dKB | 固件丢帧 %d"
                % (self._hb.uptime_ms / 1000.0, self._hb.peer_count,
                   self._hb.free_heap // 1024, self._hb.dropped_frames))
        elif "rows_sent" in src_stats:
            self.hb_label.setText("回放 %s：已发 %s/%s"
                                  % (Path(getattr(self.source, "path", "?")).name,
                                     src_stats.get("rows_sent", "-"),
                                     src_stats.get("rows_total", "-")))
        # 记录状态
        if self.store.is_recording:
            self.rec_label.setText(
                "● 记录中 %d 行 → %s" % (self.store.rows_written,
                                         self.store.csv_path.name
                                         if self.store.csv_path else ""))
        else:
            self.rec_label.setText("○ 未记录")
            self.rec_label.setStyleSheet("color:#888;")
        # 卡片新鲜度
        for mac, card in self._cards.items():
            ts = self._last_seen.get(mac)
            card.set_stale(ts is None or now - ts > STALE_AFTER_S)
        # 区域俯视图更新
        self._update_zone_map(now)

    def _update_zone_map(self, now: float):
        """计算三链路归一化响应，驱动区域俯视图（粗粒度定位）。"""
        responses: dict[str, float] = {}
        for mac, s in self._series.items():
            if not s["var"]:
                continue
            # 最近 2s 窗口的 var 均值
            cutoff = now - 2.0
            recent = [v for t, v in zip(s["t"], s["var"]) if t >= cutoff]
            if not recent:
                recent = [s["var"][-1]]
            avg = sum(recent) / len(recent)
            # 噪声底：优先标定值；否则用 120s 滚动最小值 ×1.25 作代理
            hist = self._var_hist.setdefault(mac, deque(maxlen=240))
            hist.append(avg)
            nf = self._noise_floor.get(mac)
            if not nf:
                nf = max(1.0, min(hist) * 1.25)
            # 归一化响应：涨幅超出噪声底的倍数，封顶 10 防单链路爆表
            r = max(0.0, avg / nf - 1.0)
            responses[mac] = min(10.0, r)
        self.zone_map.update_activity(responses, self._names)

    # ------------------------------------------------------------ 关闭 ------

    def closeEvent(self, event):
        for t in (self._poll_timer, self._plot_timer, self._stat_timer):
            t.stop()
        if self._calib:
            self._cancel_calib(self._calib["mac"])
        self._stop_source()
        if self.store.is_recording:
            self.store.stop_session()
        super().closeEvent(event)


class _ReplayAdapter:
    """把 CsvReplayer 包成数据源鸭子接口（无 frame_queue，产 Record 入队）。"""

    def __init__(self, path: str, speed: float, record_queue: "queue.Queue"):
        import replay as replay_mod
        self.path = Path(path)
        self.speed = speed
        self._rp = replay_mod.CsvReplayer(
            path, on_record=lambda rec: _put_queue(record_queue, rec),
            speed=speed)

    def start(self):
        self._rp.start()

    def stop(self):
        self._rp.stop()

    def stats(self) -> dict:
        return self._rp.stats()


def _put_queue(q: "queue.Queue", rec: Record):
    try:
        q.put_nowait(rec)
    except queue.Full:
        pass


def src_desc(src) -> str:
    cls = type(src).__name__
    if hasattr(src, "port") and hasattr(src, "baud"):
        return "%s %s@%d" % (cls, src.port, src.baud)
    if hasattr(src, "host"):
        return "%s %s:%s" % (cls, src.host, getattr(src, "port", ""))
    if hasattr(src, "path"):
        return "%s %s (%.1fx)" % (cls, src.path, getattr(src, "speed", 1.0))
    return cls


# ===========================================================================
# 入口
# ===========================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CIS 桌面监控主界面（M3）")
    ap.add_argument("--source", choices=["serial", "udp", "replay", "sim"],
                    default="serial", help="数据源（默认串口）")
    ap.add_argument("--port", default="COM3", help="串口号（默认 COM3）")
    ap.add_argument("--baud", type=int, default=2_000_000, help="波特率（默认 2M）")
    ap.add_argument("--udp-host", default="0.0.0.0")
    ap.add_argument("--udp-port", type=int, default=3333)
    ap.add_argument("--replay-file", default=None, help="回放 CSV 路径")
    ap.add_argument("--replay-speed", type=float, default=4.0)
    ap.add_argument("--data-root", default=None,
                    help="CSV 数据根目录（默认 <工程>/data）")
    ap.add_argument("--auto-record", action="store_true",
                    help="连上后自动开始记录")
    ap.add_argument("--auto-start", action="store_true", help="启动即连接数据源")
    ap.add_argument("--auto-scan", action="store_true",
                    help="启动时自动扫描串口识别 RX 板")
    ap.add_argument("--title", default=None, help="窗口标题（冒烟用）")
    args = ap.parse_args(argv)

    app = QApplication.instance() or QApplication(sys.argv)

    # ---- 自动扫描（可选） ----
    device_info = None
    if args.auto_scan and args.source == "serial":
        from splash import run_splash_scan
        rx_ports, choice = run_splash_scan(baud=args.baud)
        if choice == "exit":
            return 0
        if rx_ports:
            args.port = rx_ports[0]
            args.auto_start = True
            # 获取探测结果（从 splash 模块取）
            from self_test import probe_port
            device_info = probe_port(args.port, args.baud, timeout_s=1.5)

    win = Dashboard(args)
    if device_info is not None:
        win._device_info = device_info
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
