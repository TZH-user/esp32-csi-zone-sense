"""Parse M1 sample log: CSI line stats (MAC hits, inter-arrival, amplitude stability)."""
import re, sys, statistics

path = sys.argv[1]
pat = re.compile(r"I \((\d+)\) cis_rx: CSI mac=([0-9a-f:]+) amp=([\d.]+) amp_p2p=(\d+) rssi=(-?\d+) agc=(-?\d+) jitter=([0-9.]+) wander=([0-9.]+)")

lines = open(path, 'r', errors='replace').read().splitlines()
csi, other = [], []
for ln in lines:
    m = pat.search(ln)
    if m:
        csi.append(m.groups())
    elif ln.strip():
        other.append(ln.strip())

macs = {}
for g in csi:
    macs[g[1]] = macs.get(g[1], 0) + 1

ts = [int(g[0]) for g in csi]
amp = [float(g[2]) for g in csi]
amp_p2p = [int(g[3]) for g in csi]
rssi = [int(g[4]) for g in csi]
agc = [int(g[5]) for g in csi]
jitter = [float(g[6]) for g in csi]

iv = [b - a for a, b in zip(ts, ts[1:])]

def stats(v):
    if not v:
        return "n/a"
    return "min=%s max=%s mean=%.2f std=%.3f p2p=%s" % (min(v), max(v), statistics.mean(v), statistics.pstdev(v), max(v) - min(v))

span = ts[-1] - ts[0] if len(ts) > 1 else 0
print("total_lines=%d  csi_lines=%d  non_empty_other_lines=%d" % (len(lines), len(csi), len(other)))
for ln in other[:15]:
    print("  OTHER: " + ln)
print("mac_histogram=%s" % macs)
print("time_span_ms=%d  effective_rate=%.2f Hz" % (span, len(csi) * 1000.0 / span if span else 0))
if iv:
    iv_sorted = sorted(iv)
    print("inter_arrival_ms: min=%d p50=%d p95=%d max=%d mean=%.1f" % (
        iv_sorted[0], iv_sorted[len(iv_sorted) // 2], iv_sorted[int(len(iv_sorted) * 0.95)], iv_sorted[-1], statistics.mean(iv)))
    gaps = [(a, b, d) for a, b, d in zip(ts, ts[1:], iv) if d > 300]
    print("gaps>300ms: %d %s" % (len(gaps), gaps[:10]))
print("amp: %s" % stats(amp))
print("amp_p2p(milli): %s" % stats(amp_p2p))
print("rssi: %s" % stats(rssi))
print("agc_histogram=%s" % {v: agc.count(v) for v in set(agc)})
print("jitter: %s" % stats(jitter))
