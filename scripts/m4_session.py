"""M4 多 peer 联调驱动：COM3@2M 采集 + 计划 REPL 命令 + 可选 TX 复位注入。

Usage:
  python m4_session.py <port> <baud> <seconds> <rawlog> [item@t ...]
    item = "peer add 1a:.. zone1@10"   -> REPL 命令，t 秒时发送（\\n 结尾）
    item = "!esptool COM5@70"          -> t 秒时用 esptool chip_id 复位该 TX 板
                                          （下载模式+硬复位 ≈ 链路静默 10~18s）

相对 m2_verify.py 的增强：
  1. 串口异常自动重连（RX 重启后 USB-Serial-JTAG 重新枚举）；
  2. 控制台日志行提取（peer list / train done / restored peer / got ip / rst 等
     关键行带时间戳打印，二进制帧字节按不可打印字符过滤掉）；
  3. TX 复位注入（!esptool），用于无人遮挡条件下的"单链路静默"串扰观测。
"""
import serial, time, sys, struct, subprocess, re

SYNC = b'\xAA\x55'
ESPTOOL = r'C:\Espressif\tools\python\v5.5.6\venv\Scripts\esptool.exe'

CONSOLE_MARKERS = [
    'peer[', 'peer(s)', 'OK: peer added', 'peer already exists', 'peer add failed',
    'OK: peer deleted', 'peer del failed', 'usage:', 'command error', 'ret=',
    'restored peer', 'peer table loaded', 'no valid peer table',
    'train start', 'training started', 'train done', 'training ',
    'sta got IP', 'got ip:', 'esp-radar started', 'RX ready',
    'rst:', 'boot: ESP-IDF', 'Guru Meditation', 'assert', 'abort()',
    'esp_radar_start failed', 'created peer ctx', 'updated peer ctx',
    'removed peer ctx', 'radar stopped', 'state=', 'unknown subcommand',
]


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def macfmt(mac: bytes) -> str:
    return ':'.join('%02x' % b for b in mac)


def main():
    port_name, baud, seconds, out_path = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
    items = []
    for a in sys.argv[5:]:
        body, _, t = a.rpartition('@')
        items.append((float(t), body))
    items.sort()

    port = None
    t0 = time.time()
    next_item = 0
    buf = bytearray()
    line_buf = ''
    stats = {}
    feats = {}
    events = []
    train_results = []
    markers = []           # (t, kind, text)
    bad_crc = resyncs = reconnects = 0
    esptool_procs = []
    t_restart = None
    t_boot = None

    def stamp():
        return time.time() - t0

    def note(kind, text):
        markers.append((stamp(), kind, text))
        print('[%7.2fs] | %s' % (stamp(), text), flush=True)

    def open_port():
        return serial.Serial(port_name, baud, timeout=0.2)

    try:
        port = open_port()
    except Exception as e:
        print('initial open failed: %r — will retry in loop' % e, flush=True)

    with open(out_path, 'wb') as f:
        while stamp() < seconds:
            now = stamp()
            # scheduled items
            while next_item < len(items) and items[next_item][0] <= now:
                _, body = items[next_item]
                next_item += 1
                if body.startswith('!esptool '):
                    p = body.split()[1]
                    print('[%7.2fs] >>> esptool reset %s (background)' % (now, p), flush=True)
                    esptool_procs.append(subprocess.Popen(
                        [ESPTOOL, '--port', p, 'chip_id'],
                        stdout=open(out_path + '.esptool.log', 'ab'),
                        stderr=subprocess.STDOUT))
                else:
                    if body == 'restart':
                        t_restart = now
                    print('[%7.2fs] >>> %r' % (now, body), flush=True)
                    try:
                        port.write((body + '\n').encode())
                    except Exception as e:
                        print('    write failed: %r' % e, flush=True)

            # read
            try:
                if port is None:
                    raise serial.SerialException('not open')
                data = port.read(8192)
            except Exception:
                if port is not None:
                    try:
                        port.close()
                    except Exception:
                        pass
                port = None
                time.sleep(0.3)
                try:
                    port = open_port()
                    reconnects += 1
                    print('[%7.2fs] *** serial reconnected (#%d)' % (stamp(), reconnects), flush=True)
                except Exception:
                    continue
                continue

            if data:
                f.write(data)
                buf.extend(data)
                # console text extraction
                line_buf += data.decode('utf-8', errors='replace')
                parts = re.split(r'[\r\n]+', line_buf)
                line_buf = parts[-1]
                for ln in parts[:-1]:
                    clean = ''.join(c if 32 <= ord(c) < 127 or ord(c) > 0x2E7F else ' '
                                    for c in ln).strip()
                    if not clean:
                        continue
                    if 'rst:' in clean and t_boot is None:
                        t_boot = stamp()
                    if any(m in clean for m in CONSOLE_MARKERS):
                        note('log', clean[:160])

            # frame parse (tolerate interleaved log bytes)
            while True:
                i = buf.find(SYNC)
                if i < 0:
                    if len(buf) > 2:
                        del buf[:-1]
                    break
                if i > 0:
                    resyncs += 1
                    del buf[:i]
                if len(buf) < 6:
                    break
                ver, typ, ln = buf[2], buf[3], buf[4] | (buf[5] << 8)
                if ver != 0x01 or ln > 128:
                    del buf[:1]
                    resyncs += 1
                    continue
                if len(buf) < 8 + ln:
                    break
                payload = bytes(buf[6:6 + ln])
                crc_got = buf[6 + ln] | (buf[7 + ln] << 8)
                crc_calc = crc16_ccitt(bytes(buf[2:6 + ln]))
                del buf[:8 + ln]
                if crc_got != crc_calc:
                    bad_crc += 1
                    continue
                stats[typ] = stats.get(typ, 0) + 1
                if typ == 0x11 and ln == 17:
                    mac, ts, mean, var, p2p, rssi = struct.unpack('<6sIHHHb', payload)
                    m = macfmt(mac)
                    d = feats.setdefault(m, {'n': 0, 'first': stamp(), 'mean': [1 << 30, 0],
                                             'var': [1 << 30, 0], 'p2p': [1 << 30, 0],
                                             'rssi': set(), 'gap': []})
                    # frame-gap tracking (per-MAC stream continuity)
                    if d['n']:
                        d['gap'].append(stamp() - d['last'])
                    d['last'] = stamp()
                    d['n'] += 1
                    for k, v in (('mean', mean), ('var', var), ('p2p', p2p)):
                        d[k][0] = min(d[k][0], v)
                        d[k][1] = max(d[k][1], v)
                    d['rssi'].add(rssi)
                elif typ == 0x10 and ln == 15:
                    mac, name_id, state, score, rssi, ts = struct.unpack('<6sBBHbI', payload)
                    events.append((stamp(), macfmt(mac), name_id, state, score, rssi))
                    print('[%7.2fs] EVENT mac=%s name_id=%d state=%d score=%d rssi=%d' %
                          (stamp(), macfmt(mac), name_id, state, score, rssi), flush=True)
                elif typ == 0x30 and ln == 12:
                    mac, tsec, thr, noise = struct.unpack('<6sHHH', payload)
                    train_results.append((stamp(), macfmt(mac), tsec, thr, noise))
                    print('[%7.2fs] TRAIN_RESULT mac=%s train_s=%d thr=%d noise=%d' %
                          (stamp(), macfmt(mac), tsec, thr, noise), flush=True)

    for p in esptool_procs:
        p.wait()

    print('\n==== M4 session summary ====')
    print('reconnects: %d, bad_crc: %d, resyncs: %d' % (reconnects, bad_crc, resyncs))
    print('restart_sent_at: %s, boot_line_at: %s' % (
        '%.2fs' % t_restart if t_restart is not None else 'n/a',
        '%.2fs' % t_boot if t_boot is not None else 'n/a'))
    for t in sorted(stats):
        print('type 0x%02x: %d frames' % (t, stats[t]))
    for m, d in sorted(feats.items()):
        gaps = d['gap']
        print('feature %s: n=%d first_at=%.2fs mean=[%d..%d] var=[%d..%d] p2p=[%d..%d] rssi=%s'
              % (m, d['n'], d['first'], d['mean'][0], d['mean'][1], d['var'][0],
                 d['var'][1], d['p2p'][0], d['p2p'][1], sorted(d['rssi'])))
        if gaps:
            gaps_s = sorted(gaps)
            print('    frame gap p50=%.3fs max=%.3fs (n=%d)' %
                  (gaps_s[len(gaps_s) // 2], gaps_s[-1], len(gaps_s)))
    for tr in train_results:
        print('train_result: t=%.2fs mac=%s train_s=%d thr=%d noise=%d' % tr)
    for e in events:
        print('event: t=%.2fs mac=%s name_id=%d state=%d score=%d rssi=%d' % e)
    print('---- console markers ----')
    for t, kind, text in markers:
        print('[%7.2fs] %s' % (t, text))


if __name__ == '__main__':
    main()
