"""M4 离线分析：解析 m4_session.py 的原始串口日志（0xAA55 帧 + 控制台行）。
Usage: python m4_analyze.py <rawlog>
"""
import sys, struct, re

SYNC = b'\xAA\x55'
ESPTOOL_MARK = 70  # not used offline; TX-reset windows read from gaps


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
    raw = open(sys.argv[1], 'rb').read()
    buf = bytearray()
    stats = {}
    feats = {}
    events = []
    trains = []
    bad_crc = resyncs = 0
    lines = []
    for b in raw:
        pass
    # console lines: split raw on newlines, filter printable runs
    text = raw.decode('utf-8', errors='replace')
    for ln in re.split(r'[\r\n]+', text):
        clean = ''.join(c if 32 <= ord(c) < 127 or ord(c) > 0x2E7F else ' ' for c in ln).strip()
        if clean:
            lines.append(clean)

    # frames
    buf = bytearray(raw)
    while True:
        i = buf.find(SYNC)
        if i < 0:
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
            d = feats.setdefault(macfmt(mac), {'n': 0, 'var': [1 << 30, 0], 'ts': []})
            d['n'] += 1
            d['var'][0] = min(d['var'][0], var)
            d['var'][1] = max(d['var'][1], var)
            d['ts'].append(ts)
        elif typ == 0x10 and ln == 15:
            mac, name_id, state, score, rssi, ts = struct.unpack('<6sBBHbI', payload)
            events.append((macfmt(mac), name_id, state, score, rssi))
        elif typ == 0x30 and ln == 12:
            mac, tsec, thr, noise = struct.unpack('<6sHHH', payload)
            trains.append((macfmt(mac), tsec, thr, noise))

    print('bad_crc=%d resyncs=%d' % (bad_crc, resyncs))
    for t in sorted(stats):
        print('type 0x%02x: %d frames' % (t, stats[t]))
    for m, d in sorted(feats.items()):
        print('feature %s: n=%d var=[%d..%d]' % (m, d['n'], d['var'][0], d['var'][1]))
        gaps = []
        ts = d.get('ts', [])
        for a, b in zip(ts, ts[1:]):
            g = (b - a) & 0xFFFFFFFF
            if g < 60000:            # 忽略跨 uptime 环回
                gaps.append(g)
        if gaps:
            top = sorted(gaps)[-5:]
            print('    ts_ms gaps: top5=%s (n=%d)' % (top, len(gaps)))
            big = [(a, b, (b - a) & 0xFFFFFFFF) for a, b in zip(ts, ts[1:])
                   if 2000 <= ((b - a) & 0xFFFFFFFF) < 60000]
            for a, b, g in big:
                print('    gap window: uptime %dms -> %dms (%d ms silent)' % (a, b, g))
    for tr in trains:
        print('train_result(0x30): mac=%s train_s=%d thr=%d noise=%d' % tr)
    ev_by = {}
    for e in events:
        ev_by.setdefault((e[0], e[2]), []).append(e)
    for k in sorted(ev_by):
        lst = ev_by[k]
        states = sorted(set(e[2] for e in lst))
        print('events mac=%s: n=%d states=%s scores=[%d..%d]' %
              (k[0], len(lst), states, min(e[3] for e in lst), max(e[3] for e in lst)))
    print('---- interesting console lines ----')
    pat = re.compile(r'peer\[|peer\(s\)|peer added|peer deleted|peer del|train (start|done)|'
                     r'thresholds persisted|restored peer|peer table loaded|no valid peer|'
                     r'sta got IP|RX ready|esp-radar started|rst:|Guru|abort|assert')
    for ln in lines:
        if pat.search(ln):
            print(ln[:170])


if __name__ == '__main__':
    main()
