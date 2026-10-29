# -*- coding: utf-8 -*-
"""CIS 上位机自检与身份识别。

启动时扫描串口，逐端口探测，识别出 CIS RX 板并返回设备信息。
识别逻辑：
  1. 强识别：读到的文本输出含 "ESP32_CIS" → 固件 banner 确证
  2. 弱识别：2M 波特率打开成功 + 收到 CRC 通过的心跳帧（type=0x01）
  3. 其他：能打开但不是 CIS → unknown
  4. 打开失败 → fail

用法：
    ports = scan_ports()
    for info in probe_all(ports):
        print(info)
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field

import serial
from serial.tools import list_ports

# 复用协议层（避免重写 CRC/解析）
import protocol


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class PortInfo:
    """端口信息（扫描阶段）。"""
    port: str
    description: str = ""
    hwid: str = ""
    is_bt: bool = False

    def __post_init__(self):
        if not self.description:
            self.description = ""
        if not self.hwid:
            self.hwid = ""
        self.is_bt = ("蓝牙" in self.description
                      or "bluetooth" in self.description.lower()
                      or "BTHENUM" in self.hwid.upper()
                      or "BT " in self.description.upper())


@dataclass
class ProbeResult:
    """单端口探测结果。"""
    port: str
    status: str = "unknown"         # "rx" | "rx_heartbeat" | "tx" | "unknown" | "fail"
    banner: str = ""                # 固件 banner 片段（首 120 字符）
    mac: str | None = None          # 从心跳/特征帧解出的源 MAC
    uptime_ms: int | None = None
    free_heap: int | None = None
    peer_count: int | None = None
    crc_ok: int = 0                 # 收到的 CRC 通过帧数
    baud: int = 0
    error: str = ""                 # 失败时的错误信息
    commands: list[str] = field(default_factory=list)   # help 命令返回的命令列表
    board_type: str = ""            # "RX" | "TX1" | "TX2" | "TX3" | ""
    board_name: str = ""            # 设备名称（经典#1 TX1、C3 TX2 等）

    # 已知 MAC → 设备名称映射（按源 MAC 后字节）
    MAC_MAP: dict[str, tuple[str, str]] = field(default_factory=lambda: {
        "00": ("TX1", "经典#1 TX1"),     # 1a:00:00:00:00:00
        "01": ("TX2", "C3 TX2"),         # 1a:00:00:00:00:01
        "02": ("TX3", "经典#2 TX3"),     # 1a:00:00:00:00:02
        "03": ("S3", "S3 RX"),           # 1a:00:00:00:00:03（mesh 后 S3 也发源帧）
    })

    @property
    def is_rx(self) -> bool:
        return self.status in ("rx", "rx_heartbeat")

    @property
    def device_name(self) -> str:
        """根据 MAC 或 board_name 返回设备名称。"""
        if self.board_name:
            return self.board_name
        if self.mac:
            # MAC 格式: "1a:00:00:00:00:00" 或 "1a:00:00:00:00:00"
            mac = self.mac.replace(":", "").lower()
            last_byte = mac[-2:]
            if last_byte in self.MAC_MAP:
                return self.MAC_MAP[last_byte][1]
        return ""

    @property
    def is_rx(self) -> bool:
        return self.status in ("rx", "rx_heartbeat")

    def summary(self) -> str:
        tag = {"rx": "✓ S3 接收节点",
               "rx_heartbeat": "✓ S3 接收节点（已运行）",
               "tx": "✓ 发送节点",
               "unknown": "— 未识别",
               "fail": " 打开失败"}[self.status]
        s = "%s: %s" % (self.port, tag)
        # 显示设备名称
        if self.board_name:
            s += " [%s]" % self.board_name
        # banner 只取 ASCII 可打印部分，过滤二进制帧
        if self.banner:
            clean = _clean_text(self.banner, max_len=120)
            if clean.strip():
                s += "  [%s]" % clean.strip()
        parts = []
        if self.mac:
            parts.append("MAC=%s" % self.mac)
        if self.uptime_ms is not None:
            parts.append("uptime=%.1fs" % (self.uptime_ms / 1000))
        if self.free_heap is not None:
            parts.append("heap=%d" % self.free_heap)
        if self.peer_count is not None:
            parts.append("peers=%d" % self.peer_count)
        if parts:
            s += "  " + ", ".join(parts)
        if self.error:
            s += "  (%s)" % self.error
        return s


def _clean_text(text: str, max_len: int = 120) -> str:
    """清理文本：只保留 ASCII 可打印 + 中文，去掉二进制帧数据。（备用）"""
    # 调用 _extract_text 的同类逻辑，但这里接收的是 str 不是 bytes
    out = []
    for c in text:
        code = ord(c)
        if code < 0x20 and code not in (0x09, 0x0A, 0x0D):
            continue
        if 0x7F <= code <= 0x9F:
            continue
        if code >= 0x20 or code > 0xA0:
            out.append(c)
    result = "".join(out)
    if len(result) > max_len:
        result = result[:max_len].rstrip()
    return result


# ---------------------------------------------------------------------------
# 端口扫描
# ---------------------------------------------------------------------------

def scan_ports() -> list[PortInfo]:
    """扫描系统串口，过滤蓝牙与 CIS_SCAN_SKIP 名单，返回 PortInfo 列表。

    CIS_SCAN_SKIP：逗号分隔串口号（如 "COM19"）——探测会真实打开端口并拉
    DTR/RTS，非本系统的设备（如用户自用的鲁班猫串口）必须先排除，避免打扰。
    """
    skip = {s.strip().upper() for s in os.environ.get("CIS_SCAN_SKIP", "").split(",")
            if s.strip()}
    out = []
    for p in list_ports.comports():
        if p.device.upper() in skip:
            continue
        if "蓝牙" in (p.description or "") or "Bluetooth" in (p.description or ""):
            continue
        out.append(PortInfo(port=p.device, description=p.description, hwid=p.hwid))
    return out


# ---------------------------------------------------------------------------
# 单端口探测
# ---------------------------------------------------------------------------

def probe_port(port: str, baud: int = 2_000_000, timeout_s: float = 2.0) -> ProbeResult:
    """打开端口 → 读文本 → 解析心跳 → 识别设备。

    步骤：
    1. 先用 115200 读启动文本（TX 固件串口输出，如果有的话）
    2. 再以 2M 读后续数据（RX 心跳帧/CIS 协议）
    3. 如果端口在 TX 映射表中，直接返回 TX 设备信息（不依赖固件输出）
    4. 否则用文本内容识别 RX/TX
    """
    res = ProbeResult(port=port, baud=baud)

    # TX 端口映射（固定，不依赖固件输出）
    TX_PORT_MAP = {
        "COM15": ("TX1", "经典#1 TX1"),
        "COM5": ("TX2", "C3 TX2"),
        "COM17": ("TX3", "经典#2 TX3"),
    }

    # 如果端口在 TX 映射表中，直接返回 TX 设备信息
    if port in TX_PORT_MAP:
        board_type, board_name = TX_PORT_MAP[port]
        res.status = "tx"
        res.board_type = board_type
        res.board_name = board_name
        res.crc_ok = 0
        return res

    # 不是 TX 端口，继续探测 RX
    ser_115k: serial.Serial | None = None
    ser_2m: serial.Serial | None = None
    try:
        # 阶段 1：用 115200 读启动文本（前 1 秒）
        ser_115k = serial.Serial(port, 115200, timeout=1.0)
        time.sleep(0.5)
        start_text = b""
        deadline_115k = time.monotonic() + 1.0
        while time.monotonic() < deadline_115k:
            chunk = ser_115k.read(1024)
            if chunk:
                start_text += chunk
        ser_115k.close()
        ser_115k = None

        start_text_str = start_text.decode("utf-8", errors="replace")
        res.banner = start_text_str[:200].replace("\r", "").replace("\n", " ").strip()

        # 阶段 2：用 2M 读后续数据
        ser_2m = serial.Serial(port, baud, timeout=0.1)
        deadline = time.monotonic() + timeout_s
        raw_chunks = []
        parser = protocol.StreamParser()
        text_chunks = []

        while time.monotonic() < deadline:
            chunk = ser_2m.read(512)
            if chunk:
                raw_chunks.append(chunk)
                frames = parser.feed(chunk)
                _extract_info(res, frames)
                text = _extract_text(chunk)
                text_chunks.append(text)

        res.banner = "".join(text_chunks)[:200].replace("\r", "").replace("\n", " ").strip()

        # 识别
        text = start_text_str + "".join(text_chunks)
        text = text[:400].replace("\r", "").replace("\n", " ")

        # RX 识别：固件文本含 "ESP32_CIS" 或有合法心跳帧
        if "ESP32_CIS" in text or "ESP32_CIS" in res.banner:
            res.status = "rx"
            res.board_type = "RX"
            res.board_name = "S3 接收节点"
        elif res.uptime_ms is not None or parser.frames_ok > 0:
            res.status = "rx_heartbeat"
            res.board_type = "RX"
            res.board_name = "S3 接收节点"
        else:
            res.status = "unknown"
        res.crc_ok = parser.frames_ok

    except serial.SerialException as exc:
        res.status = "fail"
        res.error = str(exc).split("\n")[0][:80]
    except Exception as exc:
        res.status = "fail"
        res.error = "%s: %s" % (type(exc).__name__, str(exc)[:60])
    finally:
        if ser_115k is not None:
            try:
                ser_115k.close()
            except Exception:
                pass
        if ser_2m is not None:
            try:
                ser_2m.close()
            except Exception:
                pass
    return res


def _extract_text(raw_bytes: bytes) -> str:
    """从原始字节中提取 ASCII 文本，跳过二进制帧结构。

    CIS 帧：AA 55 | ver | type | len(2B LE) | payload | crc(2B LE)
    这里启发式：保留可打印 ASCII + 中文，跳过魔数 0xAA 0x55 及其后的帧头/payload。
    """
    out = []
    i = 0
    while i < len(raw_bytes):
        b = raw_bytes[i]

        # 检查魔数 0xAA 0x55
        if b == 0xAA and i + 1 < len(raw_bytes) and raw_bytes[i + 1] == 0x55:
            # 跳过整个帧：魔数 2B + 头 4B (ver+type+len) + payload + CRC 2B
            # payload 长度从 len 字段读取（little-endian）
            if i + 6 < len(raw_bytes):
                plen = raw_bytes[i + 4] | (raw_bytes[i + 5] << 8)
                frame_len = 2 + 4 + plen + 2  # magic + header + payload + crc
            else:
                frame_len = 20  # 启发式最小帧长
            i += frame_len
            continue

        # 控制字符过滤
        if b < 0x20 and b not in (0x09, 0x0A, 0x0D):
            i += 1
            continue

        # 可打印 ASCII + 中文/扩展
        if b >= 0x20 or b > 0xA0:
            out.append(chr(b))
        i += 1

    return "".join(out)


def _extract_info(res: ProbeResult, frames: list[protocol.Frame]):
    """从帧中提取设备信息（MAC / uptime / heap / peer_count）。
    0x12/0x13/0x14 多节点帧同样纳入识别与 parser.frames_ok 自检统计。"""
    for f in frames:
        if f.type == protocol.TYPE_HEARTBEAT:
            try:
                hb = protocol.decode_payload(f)
                res.uptime_ms = hb.uptime_ms
                res.free_heap = hb.free_heap
                res.peer_count = hb.peer_count
            except Exception:
                pass
        elif f.type in (protocol.TYPE_FEATURES, protocol.TYPE_FEATURE_MULTI):
            try:
                feat = protocol.decode_payload(f)
                if res.mac is None:
                    res.mac = protocol.format_mac(feat.mac)
            except Exception:
                pass
        elif f.type in (protocol.TYPE_EVENT, protocol.TYPE_EVENT_MULTI):
            try:
                ev = protocol.decode_payload(f)
                if res.mac is None:
                    res.mac = protocol.format_mac(ev.mac)
            except Exception:
                pass
        elif f.type == protocol.TYPE_CALIB_MULTI:
            try:
                c = protocol.decode_payload(f)
                if res.mac is None:
                    res.mac = protocol.format_mac(c.mac)
            except Exception:
                pass


def probe_all(ports: list[PortInfo], baud: int = 2_000_000,
              timeout_s: float = 2.0) -> list[ProbeResult]:
    """逐端口探测，返回结果列表（按端口号排序）。"""
    out = []
    for p in ports:
        if p.is_bt:
            continue
        out.append(probe_port(p.port, baud, timeout_s))
    return out


# ---------------------------------------------------------------------------
# 快捷自检（连接后发送 help 命令）
# ---------------------------------------------------------------------------

def quick_selftest(port: str, baud: int = 2_000_000, timeout_s: float = 1.5) \
        -> dict:
    """连接端口 → 发 help → 收集命令列表。返回 {ok, commands, raw}。"""
    out = {"ok": False, "commands": [], "raw": ""}
    ser: serial.Serial | None = None
    try:
        ser = serial.Serial(port, baud, timeout=0.1)
        time.sleep(0.3)
        ser.write(b"help\r\n")
        deadline = time.monotonic() + timeout_s
        chunks = []
        while time.monotonic() < deadline:
            chunk = ser.read(256)
            if chunk:
                chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        out["raw"] = text[:500]
        # 解析命令名：找 "peer / train / threshold / ..." 模式
        cmds = []
        for kw in ("peer", "train", "threshold", "report", "udp", "dump",
                    "wifi_status"):
            if kw in text:
                cmds.append(kw)
        out["commands"] = cmds
        out["ok"] = "CIS" in text or len(cmds) >= 3
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)[:80]
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
    return out


def query_peer_list(port: str, baud: int = 2_000_000, timeout_s: float = 2.0) \
        -> dict:
    """连接端口 → 发 peer list → 解析已注册 peer（MAC + name）。

    返回：{peers: [(mac, name), ...], raw: str}
    """
    out = {"peers": [], "raw": ""}
    ser: serial.Serial | None = None
    try:
        ser = serial.Serial(port, baud, timeout=0.1)
        time.sleep(0.5)
        ser.write(b"peer list\r\n")
        deadline = time.monotonic() + timeout_s
        chunks = []
        while time.monotonic() < deadline:
            chunk = ser.read(8192)
            if chunk:
                chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        out["raw"] = text[:2000]

        # 解析 peer 行：peer[0] 1a:00:00:00:00:00 name=zone1 ...
        import re
        pattern = r'peer\[\d+\]\s+([0-9a-f:]+)\s+name=(\S+)'
        for m in re.finditer(pattern, text):
            mac = m.group(1)
            name = m.group(2)
            out["peers"].append((mac, name))
    except Exception as exc:
        out["error"] = str(exc)[:80]
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
    return out


# ---------------------------------------------------------------------------
# CLI 入口（调试用）
# ---------------------------------------------------------------------------

def main():
    print("=== CIS 串口自检 ===")
    ports = scan_ports()
    print("扫描到 %d 个串口：" % len(ports))
    for p in ports:
        bt = " (蓝牙，跳过)" if p.is_bt else ""
        print("  %s  — %s%s" % (p.port, p.description, bt))
    print()

    non_bt = [p for p in ports if not p.is_bt]
    if not non_bt:
        print("无非蓝牙串口可探测。")
        return

    print("逐端口探测（2M 波特率，每端口 2s）...")
    results = probe_all(non_bt)
    rx_found = []
    for r in results:
        print("  " + r.summary())
        if r.is_rx:
            rx_found.append(r)

    print()
    if rx_found:
        print("找到 %d 块 CIS RX 板：" % len(rx_found))
        for r in rx_found:
            print("  %s  %s" % (r.port, r.banner[:60]))
            if r.mac:
                print("    MAC=%s  uptime=%.1fs  heap=%d  peers=%d" % (
                    r.mac, (r.uptime_ms or 0) / 1000,
                    r.free_heap or 0, r.peer_count or 0))
    else:
        print("未找到 CIS RX 板。请检查：")
        print("  - 板子是否上电并接 USB")
        print("  - 波特率是否为 2,000,000（默认）")
        print("  - 固件是否为 ESP32_CIS_V1.0")


if __name__ == "__main__":
    main()
