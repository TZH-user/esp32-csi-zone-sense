"""CIS UDP 数据源：监听 0.0.0.0:3333，解析与串口同款 0xAA55 二进制帧。

接口与 serial_source.SerialSource 一致（IMPLEMENTATION_PLAN.md §6 udp_source
"同接口 UDP 监听（3333）"）：
    src = UdpSource(host="0.0.0.0", port=3333)
    src.start()                 # 绑定失败直接抛异常给调用者
    src.frame_queue             # queue.Queue[protocol.Frame]
    src.stats()                 # 计数快照（报文/字节/解析/队列/固件心跳）
    src.stop()                  # 幂等
    src.send_line(text) -> False  # UDP 单向无命令通道，恒 False

线程模型：单 recv 线程阻塞 recvfrom → StreamParser（CRC/重同步，与串口同款）
→ frame_queue。队满丢帧计数、EVENT 优先挤掉非 EVENT——与 SerialSource 相同
策略。无环形缓冲：UDP 报文由内核 socket 缓冲，天然队列。

已知部署事实（计划 v1.3 §3）：当前 PC 与节点分属 192.168.1.x/2.x 网段，
UDP 3333 从本 PC 不可达，上位机需接 1003-k LAN 或连 RX SoftAP 后本源才通。
本模块不涉及网络可达性，只负责"通了之后"的解析。
"""

from __future__ import annotations

import queue
import socket
import threading
import time

import protocol


class UdpSource:
    """UDP → StreamParser → frame_queue。"""

    def __init__(self, host: str = "0.0.0.0", port: int = 3333,
                 queue_size: int = 2000,
                 parser: protocol.StreamParser | None = None):
        self.host = host
        self.port = port
        self.parser = parser if parser is not None else protocol.StreamParser()
        self.frame_queue: "queue.Queue[protocol.Frame]" = queue.Queue(maxsize=queue_size)
        self.last_heartbeat = None
        self.last_error = None
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = {"datagrams": 0, "bytes_recv": 0, "recv_errors": 0,
                       "queue_dropped": 0, "send_attempts": 0}

    # ---- 生命周期 ---------------------------------------------------------

    def start(self):
        if self._thread is not None:
            raise RuntimeError("UdpSource already started")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.2)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._recv_loop,
                                        name="cis-udp-recv", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
            self._thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    # ---- 收包线程 ---------------------------------------------------------

    def _recv_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    break
                self._stats["recv_errors"] += 1
                self.last_error = exc
                time.sleep(0.05)
                continue
            if not data:
                continue
            self._stats["datagrams"] += 1
            self._stats["bytes_recv"] += len(data)
            for frame in self.parser.feed(data):
                self._capture_heartbeat(frame)
                self._enqueue(frame)

    # ---- 帧入队与统计 -----------------------------------------------------

    def _capture_heartbeat(self, frame: protocol.Frame):
        if frame.type == protocol.TYPE_HEARTBEAT:
            try:
                hb = protocol.decode_payload(frame)
            except Exception:
                return
            self.last_heartbeat = (time.time(), hb)

    def _enqueue(self, frame: protocol.Frame):
        """队满时：EVENT 优先——挤掉队首非 EVENT 帧；其余帧直接丢弃计数。"""
        while True:
            try:
                self.frame_queue.put_nowait(frame)
                return
            except queue.Full:
                if frame.type == protocol.TYPE_EVENT:
                    try:
                        self.frame_queue.get_nowait()
                    except queue.Empty:
                        continue
                    self._stats["queue_dropped"] += 1
                else:
                    self._stats["queue_dropped"] += 1
                    return

    def send_line(self, text: str) -> bool:
        """UDP 为单向监听，无命令通道；标定/REPL 请用串口源。"""
        self._stats["send_attempts"] += 1
        return False

    def stats(self) -> dict:
        s = dict(self._stats)
        s.update(self.parser.stats())
        s["queue_depth"] = self.frame_queue.qsize()
        if self.last_heartbeat is not None:
            ts, hb = self.last_heartbeat
            s["device_dropped_frames"] = hb.dropped_frames
            s["last_heartbeat_age_s"] = round(time.time() - ts, 3)
        return s
