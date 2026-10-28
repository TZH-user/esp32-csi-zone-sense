"""CIS 串口数据源：COM3@2M 独立读线程 + ≥1MB 环形缓冲 + 丢帧计数。

接口（IMPLEMENTATION_PLAN.md §6）：
    src = SerialSource(port="COM3", baud=2_000_000)
    src.start()                 # 首次打开失败会直接抛异常给调用者
    src.frame_queue             # queue.Queue[protocol.Frame]，消费者从这里取帧
    src.stats()                 # 各级丢帧/错误计数快照
    src.stop()

线程模型：
    reader 线程  串口 →(阻塞读)→ RingBuffer（写满丢最新并计数）
    parser 线程  RingBuffer → StreamParser（重同步/去控制台噪声）→ frame_queue

丢帧计数三层，各司其职：
    ring_dropped_bytes      环满被丢弃的原始字节（parser 消费跟不上）
    queue_dropped           帧队列满被丢弃的帧（EVENT 优先挤掉非 EVENT）
    device_dropped_frames   固件心跳帧里上报的固件侧丢帧计数（0x01 payload）

依赖：纯标准库 + pyserial（IDF venv 随 esptool 自带，实测 3.5）。
"""

from __future__ import annotations

import queue
import threading
import time

import serial

import protocol


class RingBuffer:
    """定长字节环。写满时丢弃"最新"装不下的部分并计数（保旧数据完整，
    parser 消费是连续的，不会读到半截被覆盖的数据）。"""

    def __init__(self, capacity: int = 1 << 20):   # 默认 1MB
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._buf = bytearray(capacity)
        self._cap = capacity
        self._head = 0        # 写位置
        self._tail = 0        # 读位置
        self._size = 0
        self._lock = threading.Lock()
        self.dropped_bytes = 0
        self.high_water = 0

    @property
    def capacity(self) -> int:
        return self._cap

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    def write(self, data: bytes) -> int:
        """写入，返回实际存入字节数；装不下的部分计入 dropped_bytes。"""
        data = bytes(data)
        n = len(data)
        with self._lock:
            free = self._cap - self._size
            if n > free:
                self.dropped_bytes += n - free
                data = data[n - free:]      # 保最新
                n = free
            if n:
                end = self._head + n
                if end <= self._cap:
                    self._buf[self._head:end] = data
                else:
                    first = self._cap - self._head
                    self._buf[self._head:] = data[:first]
                    self._buf[:end - self._cap] = data[first:]
                self._head = end % self._cap
                self._size += n
                if self._size > self.high_water:
                    self.high_water = self._size
            return n

    def read(self, want: int = 4096) -> bytes:
        """非阻塞读出至多 want 字节（连续区域，绝不回绕重叠）。"""
        with self._lock:
            n = min(want, self._size)
            if n <= 0:
                return b""
            end = self._tail + n
            if end <= self._cap:
                out = bytes(self._buf[self._tail:end])
            else:
                first = self._cap - self._tail
                out = bytes(self._buf[self._tail:]) + bytes(self._buf[:end - self._cap])
            self._tail = end % self._cap
            self._size -= n
            return out


class SerialSource:
    """串口 → 环缓冲 → 帧队列。frame_queue 元素为 protocol.Frame（消费者用
    protocol.decode_payload 解出具体数据类）。"""

    def __init__(self, port: str = "COM3", baud: int = 2_000_000,
                 ring_bytes: int = 1 << 20, queue_size: int = 2000,
                 reconnect: bool = True, parser: protocol.StreamParser | None = None):
        self.port = port
        self.baud = baud
        self.reconnect = reconnect
        self.ring = RingBuffer(ring_bytes)
        self.parser = parser if parser is not None else protocol.StreamParser()
        self.frame_queue: "queue.Queue[protocol.Frame]" = queue.Queue(maxsize=queue_size)
        self.last_heartbeat = None            # (接收时刻, protocol.Heartbeat)
        self.last_error = None
        self._serial = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()   # 保护 send_line 与内部线程互斥
        self._threads: list[threading.Thread] = []
        self._stats = {
            "bytes_read": 0, "serial_errors": 0, "reconnects": 0, "queue_dropped": 0,
        }

    # ---- 生命周期 ---------------------------------------------------------

    def start(self):
        if self._threads:
            raise RuntimeError("SerialSource already started")
        self._open()                          # 首次打开失败直接抛给调用者
        t1 = threading.Thread(target=self._reader_loop, name="cis-serial-reader", daemon=True)
        t2 = threading.Thread(target=self._parser_loop, name="cis-serial-parser", daemon=True)
        self._threads = [t1, t2]
        t1.start()
        t2.start()

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []
        if self._serial is not None:
            try:
                if self._serial.is_open:
                    self._serial.close()
            except Exception:
                pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def send_line(self, text: str) -> bool:
        """向 RX 控制台写一行 REPL 命令（CRLF 结尾），返回是否成功。

        ui_main 的命令框与标定按钮经此下发 `train/threshold/peer ...`；
        串口是双向命令通道（UDP 单向、CSV 回放无通道）。ascii 编码，
        非 ASCII 字符替换不抛错。"""
        if self._stop.is_set():
            return False
        with self._write_lock:
            ser = self._serial
            if ser is None or not ser.is_open:
                return False
            try:
                ser.write((text.rstrip("\r\n") + "\r\n").encode(
                    "ascii", errors="replace"))
                ser.flush()
                return True
            except Exception as exc:
                self.last_error = exc
                return False

    # ---- 内部线程 ---------------------------------------------------------

    def _open(self):
        # 参数与 M1 实测可用的 m1_capture.py 一致（默认流控、不额外动 DTR/RTS）
        self._serial = serial.Serial(self.port, self.baud, timeout=0.1)

    def _reader_loop(self):
        while not self._stop.is_set():
            try:
                if self._serial is None or not self._serial.is_open:
                    self._open()
                    self._stats["reconnects"] += 1
                data = self._serial.read(512)
                if data:
                    self.ring.write(data)
                    self._stats["bytes_read"] += len(data)
            except Exception as exc:          # pyserial 抛 SerialException/OSError 等
                self._stats["serial_errors"] += 1
                self.last_error = exc
                if self._serial is not None:
                    try:
                        self._serial.close()
                    except Exception:
                        pass
                self._serial = None
                if not self.reconnect or self._stop.wait(1.0):
                    break

    def _parser_loop(self):
        # stop 后把环里残余帧 drain 完再退，保证不丢已收数据
        while not self._stop.is_set() or self.ring.size > 0:
            data = self.ring.read(4096)
            if not data:
                if self._stop.is_set():
                    break
                time.sleep(0.005)
                continue
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

    def stats(self) -> dict:
        s = dict(self._stats)
        s.update(self.parser.stats())
        s["ring_size"] = self.ring.size
        s["ring_high_water"] = self.ring.high_water
        s["ring_dropped_bytes"] = self.ring.dropped_bytes
        s["queue_depth"] = self.frame_queue.qsize()
        if self.last_heartbeat is not None:
            ts, hb = self.last_heartbeat
            s["device_dropped_frames"] = hb.dropped_frames
            s["last_heartbeat_age_s"] = round(time.time() - ts, 3)
        return s
