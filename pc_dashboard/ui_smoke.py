"""M3 UI 冒烟测试（IMPLEMENTATION_PLAN.md §8 M3 判据：UI 冒烟 60s 自动退退出码 0）。

流程：启动 Dashboard 主窗口（SimSource 模拟数据 + 自动记录）→ 结构检查
（区域卡片 / pyqtgraph 曲线 / 事件表 / 命令框 / 标定按钮）→ 自动点击标定按钮
验证 15s 倒计时与 0x30 回填 → 60s 时 QTimer 自动退出 → 校验 CSV 列齐全且
行数/曲线/事件表一致 → 退出码 0=全过，1=有失败项（明细打印到 stdout）。

用法：
    python ui_smoke.py                 # 60s 冒烟，弹真实窗口
    python ui_smoke.py --offscreen     # 无桌面环境下用 offscreen 平台
    python ui_smoke.py --duration 90   # 自定义时长
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from store import COLUMNS
import pyqtgraph as pg
from ui_main import Dashboard

DURATION_S = 60


class Smoke:
    def __init__(self, duration: float, offscreen: bool):
        self.duration = duration
        self.offscreen = offscreen
        self.failures: list[str] = []
        self.errors: list[str] = []          # Qt 事件循环里未捕获的异常
        self.data_root = Path(tempfile.mkdtemp(prefix="cis_smoke_"))
        self.t0 = time.monotonic()

    # ---- 检查工具 ----------------------------------------------------------

    def check(self, name: str, ok: bool, detail: str = ""):
        tag = "PASS" if ok else "FAIL"
        line = "[%s] %s%s" % (tag, name, (" — " + detail) if detail else "")
        print("%s  (%.1fs)" % (line, self.elapsed()))
        if not ok:
            self.failures.append(line)
        return ok

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    # ---- 阶段检查 ----------------------------------------------------------

    def structural(self, win: Dashboard):
        print("--- 结构检查 (%.1fs) ---" % self.elapsed())
        self.check("区域卡片 ≥2（SimSource 两 peer）", len(win._cards) >= 2,
                   "cards=%d" % len(win._cards))
        self.check("每卡片含大状态灯/score/最近事件/标定按钮",
                   all(all(hasattr(c, a) for a in
                           ("light", "score_label", "last_event_label", "calib_btn"))
                       for c in win._cards.values()))
        self.check("pyqtgraph 曲线 ≥2（每 peer 一条）",
                   len(win._curve_items) >= 2 and isinstance(win.plot, pg.PlotWidget),
                   "curves=%d" % len(win._curve_items))
        self.check("事件表存在且列头正确",
                   [win.event_table.horizontalHeaderItem(i).text()
                    for i in range(6)] == ["时间", "区域", "MAC", "事件", "score", "RSSI"])
        self.check("命令框存在且可用（Sim 有命令通道）",
                   win.cmd_edit.isEnabled() and win.cmd_btn.isEnabled())
        self.check("标定按钮存在（'标定 15s'）",
                   all(c.calib_btn.text() == "标定 15s" for c in win._cards.values()))
        # 自动点击第一张卡的标定按钮 → 开始 15s 倒计时（无卡片则留待终检失败）
        if win._cards:
            first = next(iter(win._cards.values()))
            first.calib_btn.click()
            self._clicked_mac = first.mac

    def calib_countdown(self, win: Dashboard):
        card = win._cards.get(self._clicked_mac)
        ok = (card is not None and win._calib is not None
              and win._calib.get("mac") == self._clicked_mac
              and "倒计时" in card.calib_label.text()
              and not card.calib_btn.isEnabled())
        self.check("标定倒计时已启动（15s 清场倒计时，按钮禁用）", ok,
                   "label=%r" % (card.calib_label.text() if card else None))

    def calib_backfill(self, win: Dashboard):
        card = win._cards.get(self._clicked_mac)
        ok = (card is not None and "阈值" in card.calib_label.text()
              and "噪声" in card.calib_label.text())
        self.check("0x30 回填（阈值/噪声底上卡）", ok,
                   "label=%r btn=%r" % (card.calib_label.text(),
                                        card.calib_btn.text()) if card else "card missing")

    def final(self, win: Dashboard):
        print("--- 终检 (%.1fs) ---" % self.elapsed())
        # 干净收尾：断数据源、关会话（flush CSV）
        win._stop_source()
        if win.store.is_recording:
            win.store.stop_session()
        self.check("无未捕获异常", not self.errors, "; ".join(self.errors[:3]))
        self.check("事件表有行", win.event_table.rowCount() >= 4,
                   "rows=%d" % win.event_table.rowCount())
        self.check("事件竖线已画", len(win._event_lines) >= 4,
                   "lines=%d" % len(win._event_lines))
        pts = [len(s["t"]) for s in win._series.values()]
        self.check("每 peer 曲线点数 ≥100", pts and min(pts) >= 100,
                   "points=%s" % pts)
        self.check("卡片名与 EVENT name_id 一致（区域1/区域2）",
                   sorted(c.zone_name for c in win._cards.values())
                   == ["区域1", "区域2"],
                   "names=%s" % [c.zone_name for c in win._cards.values()])
        # CSV 列齐全 + 行数
        csv_path = win.store.csv_path
        self.check("CSV 会话文件已生成", csv_path is not None and csv_path.exists(),
                   str(csv_path))
        if csv_path is not None and csv_path.exists():
            with open(csv_path, encoding="utf-8", newline="") as fh:
                reader = csv.reader(fh)
                header = next(reader)
                rows = sum(1 for _ in reader)
            self.check("CSV 列齐全（%s）" % ",".join(COLUMNS), header == COLUMNS,
                       "header=%s" % header)
            self.check("CSV 数据行 ≥600（特征流 2×10Hz + 事件 + 心跳）",
                       rows >= 600, "rows=%d" % rows)
            self.check("回放缓冲已积累", len(win.store.replay_buffer) >= 600,
                       "buffer=%d" % len(win.store.replay_buffer))
        print("--- 冒烟结束： %s ---" % ("PASS" if not self.failures else "FAIL"))

    # ---- 主流程 ------------------------------------------------------------

    def run(self) -> int:
        if self.offscreen:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication(sys.argv)

        def hook(tp, val, tb):
            self.errors.append("".join(traceback.format_exception(tp, val, tb))[-500:])
            sys.__stderr__.write("SMOKE-ERROR:\n" + self.errors[-1])
        sys.excepthook = hook

        cfg = argparse.Namespace(
            source="sim", port="", baud=2_000_000, udp_host="127.0.0.1",
            udp_port=3333, replay_file=None, replay_speed=4.0,
            data_root=self.data_root, auto_record=True, auto_start=True,
            title="CIS UI 冒烟（60s 自动退出）")
        win = Dashboard(cfg)
        win.show()
        self.check("主窗口已启动 (%.0fs QTimer 自动退出)" % self.duration,
                   win.isVisible())

        QTimer.singleShot(2000, lambda: self.structural(win))
        QTimer.singleShot(3500, lambda: self.calib_countdown(win))
        QTimer.singleShot(20000, lambda: self.calib_backfill(win))
        exit_timer = QTimer(app)
        exit_timer.setSingleShot(True)
        exit_timer.setInterval(int(self.duration * 1000))
        exit_timer.timeout.connect(lambda: app.exit(self.finish(win)))
        exit_timer.start()
        code = app.exec()
        print("SMOKE exit code=%d failures=%d" % (code, len(self.failures)))
        return code

    def finish(self, win: Dashboard) -> int:
        try:
            self.final(win)
        except Exception:
            self.errors.append(traceback.format_exc()[-500:])
            self.check("终检过程无异常", False, self.errors[-1])
        return 0 if not self.failures else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CIS UI 冒烟：60s 自动退出，退出码 0=过")
    ap.add_argument("--duration", type=float, default=DURATION_S)
    ap.add_argument("--offscreen", action="store_true",
                    help="QT_QPA_PLATFORM=offscreen（无桌面 CI 环境）")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    print("SMOKE start %s duration=%.0fs" % (datetime.now().isoformat(timespec="seconds"),
                                             args.duration))
    return Smoke(args.duration, args.offscreen).run()


if __name__ == "__main__":
    sys.exit(main())
