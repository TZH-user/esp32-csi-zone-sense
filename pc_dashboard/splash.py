# -*- coding: utf-8 -*-
"""CIS 上位机启动扫描对话框。

启动时弹出，逐端口探测，显示进度和结果。找到 RX 板后自动返回端口号；
没找到则让用户选择"继续（手动选端口）"或"退出"。

用法：
    port = run_splash_scan()   # 返回 str|None；None=用户选退出
"""

from __future__ import annotations

import sys
import threading
import time

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from self_test import PortInfo, ProbeResult, scan_ports, probe_port


# ---------------------------------------------------------------------------
# 后台扫描线程
# ---------------------------------------------------------------------------

class ScanWorker(QThread):
    """后台扫描线程，通过信号逐端口上报结果。"""
    port_found = Signal(str, str)       # (port, status_line)
    scan_done = Signal(list)            # 所有 ProbeResult 列表

    def __init__(self, baud: int = 2_000_000, timeout_s: float = 2.0):
        super().__init__()
        self.baud = baud
        self.timeout_s = timeout_s

    def run(self):
        ports = scan_ports()
        non_bt = [p for p in ports if not p.is_bt]
        results: list[ProbeResult] = []
        for p in non_bt:
            r = probe_port(p.port, self.baud, self.timeout_s)
            results.append(r)
            self.port_found.emit(r.port, r.summary())
        self.scan_done.emit(results)


# ---------------------------------------------------------------------------
# 对话框
# ---------------------------------------------------------------------------

class ScanDialog(QDialog):
    """启动扫描对话框。用户选"继续"或"退出"。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CIS 桌面监控 — 设备自检")
        self.setMinimumSize(560, 380)
        self.setModal(True)

        self._rx_ports: list[str] = []
        self._all_results: list[ProbeResult] = []
        self._user_choice: str | None = None   # "continue" | "exit" | None

        self._build_ui()

    # ---- UI 构建 ----------------------------------------------------------

    def _build_ui(self):
        vbox = QVBoxLayout(self)

        # 标题
        self.title_label = QLabel("正在扫描串口设备…")
        self.title_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        vbox.addWidget(self.title_label)

        # 端口列表
        self.port_list = QListWidget()
        self.port_list.setMinimumHeight(200)
        self.port_list.setStyleSheet("""
            QListWidget { font-family: Consolas, monospace; font-size: 12px; }
            QListWidget::item { padding: 4px; }
        """)
        vbox.addWidget(self.port_list, 1)

        # 状态栏
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: gray; font-size: 11px;")
        vbox.addWidget(self.status_label)

        # 按钮
        btn_box = QHBoxLayout()
        self.btn_continue = QPushButton("继续（手动选端口）")
        self.btn_continue.setEnabled(False)
        self.btn_continue.clicked.connect(self._on_continue)
        btn_box.addWidget(self.btn_continue)

        self.btn_exit = QPushButton("退出")
        self.btn_exit.setEnabled(False)
        self.btn_exit.clicked.connect(self._on_exit)
        btn_box.addWidget(self.btn_exit)
        vbox.addLayout(btn_box)

    # ---- 扫描逻辑 ---------------------------------------------------------

    def start_scan(self, baud: int = 2_000_000, timeout_s: float = 2.0):
        """启动后台扫描。阻塞直到用户选择。"""
        self.worker = ScanWorker(baud, timeout_s)
        self.worker.port_found.connect(self._on_port_found)
        self.worker.scan_done.connect(self._on_scan_done)
        self.worker.start()

        # 阻塞直到用户选择
        self.exec()
        return self._rx_ports, self._user_choice

    def _on_port_found(self, port: str, line: str):
        """收到一个端口探测结果。"""
        # 解析出 ProbeResult 对象（从 line 中或从参数传递）
        # 这里简化：直接用 line 显示，设备名称在 summary() 中已包含
        item = QListWidgetItem(line)
        # 根据状态着色
        if "S3 接收节点" in line or "RX" in line:
            item.setForeground(Qt.GlobalColor.darkGreen)
        elif "发送节点" in line or "TX" in line:
            item.setForeground(Qt.GlobalColor.darkBlue)
        elif "失败" in line:
            item.setForeground(Qt.GlobalColor.red)
        elif "未识别" in line or "未知" in line:
            item.setForeground(Qt.GlobalColor.darkGray)
        self.port_list.addItem(item)
        self.status_label.setText("已探测 %d 个端口…" % self.port_list.count())

    def _on_scan_done(self, results: list[ProbeResult]):
        """扫描完成。"""
        self._all_results = results
        self._rx_ports = [r.port for r in results if r.is_rx]

        if self._rx_ports:
            self.title_label.setText("找到 %d 块 CIS RX 板" % len(self._rx_ports))
            self.status_label.setText(
                "自动连接端口：%s（可改）" % ", ".join(self._rx_ports))
        else:
            self.title_label.setText("未找到 CIS RX 板")
            self.status_label.setText('请检查板子是否上电，或选"继续"手动选端口')

        self.btn_continue.setEnabled(True)
        self.btn_exit.setEnabled(True)

    def _on_continue(self):
        self._user_choice = "continue"
        self.accept()

    def _on_exit(self):
        self._user_choice = "exit"
        self.reject()


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------

def run_splash_scan(baud: int = 2_000_000, timeout_s: float = 2.0) \
        -> tuple[list[str], str | None]:
    """弹出扫描对话框，返回 (rx_ports, user_choice)。

    rx_ports: 识别到的 RX 端口列表（可能为空）
    user_choice: "continue"（继续手动选） | "exit"（退出程序） | None
    """
    dlg = ScanDialog()
    rx_ports, choice = dlg.start_scan(baud, timeout_s)
    return rx_ports, choice


if __name__ == "__main__":
    # 独立测试
    app = QApplication(sys.argv)
    rx_ports, choice = run_splash_scan()
    print("rx_ports =", rx_ports, "choice =", choice)
    sys.exit(0)
