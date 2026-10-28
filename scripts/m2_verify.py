"""M2 RX verification: capture COM3@2M, parse 0xAA55 binary frames (CRC-checked,
log-interleave tolerant), optionally send REPL commands at scheduled times.

Usage: python m2_verify.py <port> <baud> <seconds> <rawlog> [cmd1@t1 cmd2@t2 ...]
       command format: "peer add 1a:00:00:00:00:00 z1@10"  (sent with \\n at t=10s)
Prints a per-type frame summary + per-peer feature value ranges at the end.
"""
import serial, time, sys, struct

SYNC = b'\xAA\x55'

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
    cmds = []
    for a in sys.argv[5:]:
        cmd, _, t = a.rpartition('@')
        cmds.append((float(t), cmd))
    cmds.sort()

    port = serial.Serial(port_name, baud, timeout=0.2)
    buf = bytearray()
    stats = {}                      # type -> count
    bad_crc = resyncs = 0
    feats = {}                      # mac -> dict of min/max lists
    events = []
    hb_last = None
    hb_gaps = []
    t0 = time.time()
    next_cmd = 0
    raw_total = 0

    with open(out_path, 'wb') as f:
        while time.time() - t0 < seconds:
            now = time.time() - t0
            while next_cmd < len(cmds) and cmds[next_cmd][0] <= now:
                _, c = cmds[next_cmd]
                port.write((c + '\n').encode())
                print('[%6.2fs] >>> %r' % (now, c), flush=True)
                next_cmd += 1
            data = port.read(8192)
            if data:
                f.write(data)
                raw_total += len(data)
                buf.extend(data)
            # frame scan (tolerate interleaved console log bytes)
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
                if ver != 0x01 or ln > 128 or len(buf) < 8 + ln:
                    if ver != 0x01 or ln > 128:
                        del buf[:1]     # false sync, resync
                        resyncs += 1
                        continue
                    break               # wait for more
                payload = bytes(buf[6:6 + ln])
                crc_got = buf[6 + ln] | (buf[7 + ln] << 8)
                crc_calc = crc16_ccitt(bytes(buf[2:6 + ln]))
                del buf[:8 + ln]
                if crc_got != crc_calc:
                    bad_crc += 1
                    continue
                stats[typ] = stats.get(typ, 0) + 1
                if typ == 0x01 and ln == 11:
                    up, pc, heap, lost = struct.unpack('<IBIH', payload)
                    if hb_last is not None:
                        hb_gaps.append(up - hb_last)
                    hb_last = up
                elif typ == 0x11 and ln == 17:
                    mac, ts, mean, var, p2p, rssi = struct.unpack('<6sIHHHb', payload)
                    d = feats.setdefault(macfmt(mac), {'n': 0, 'mean': [1 << 30, 0], 'var': [1 << 30, 0], 'p2p': [1 << 30, 0], 'rssi': set()})
                    d['n'] += 1
                    for k, v in (('mean', mean), ('var', var), ('p2p', p2p)):
                        d[k][0] = min(d[k][0], v); d[k][1] = max(d[k][1], v)
                    d['rssi'].add(rssi)
                elif typ == 0x10 and ln == 15:
                    mac, name_id, state, score, rssi, ts = struct.unpack('<6sBBHbI', payload)
                    events.append((time.time() - t0, macfmt(mac), state, score, rssi))
                    print('[%6.2fs] EVENT mac=%s state=%d score=%d rssi=%d' %
                          (time.time() - t0, macfmt(mac), state, score, rssi), flush=True)
                elif typ == 0x30 and ln == 12:
                    mac, ts_, thr, noise = struct.unpack('<6sHHH', payload)
                    print('[%6.2fs] TRAIN_RESULT mac=%s thr=%d noise=%d' %
                          (time.time() - t0, macfmt(mac), thr, noise), flush=True)
    port.close()

    print('\n==== M2 frame summary ====')
    print('raw bytes: %d, resyncs: %d, bad_crc: %d' % (raw_total, resyncs, bad_crc))
    for t in sorted(stats):
        print('type 0x%02x: %d frames' % (t, stats[t]))
    if hb_gaps:
        print('heartbeat uptime deltas: min=%d max=%d ms (n=%d)' %
              (min(hb_gaps), max(hb_gaps), len(hb_gaps)))
    for mac, d in sorted(feats.items()):
        print('feature %s: n=%d mean=[%d..%d] var=[%d..%d] p2p=[%d..%d] rssi=%s' %
              (mac, d['n'], d['mean'][0], d['mean'][1], d['var'][0], d['var'][1],
               d['p2p'][0], d['p2p'][1], sorted(d['rssi'])))
    for e in events:
        print('event: t=%.1fs mac=%s state=%d score=%d rssi=%d' % e)

if __name__ == '__main__':
    main()
