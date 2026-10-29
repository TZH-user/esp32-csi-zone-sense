"""夜间静置数据分析 —— 生成「验收用例①：无人静置」证据报告（纯标准库）。

用法：
    python overnight_analysis.py                  # 取 data/ 下最新 *-overnight 会话
    python overnight_analysis.py <session_dir>    # 指定会话目录

输出：<session_dir>/夜间静置报告.md，核心内容：
  - 结论行：时长 / 总行数 / EVENT 误报数 / 12 链路行数离散度 / 重连次数
  - 每链路：行数、速率、mean/var 中位数与 p95/max、var>4×中位数的尖峰数
  - 每小时：各链路 var 中位数（噪声底漂移，凌晨 WiFi 负载低应可见下沉）
  - 断档：ts 间隔 > 10s 的空洞列表（对应重连/板卡重启）
  - HEARTBEAT：条数与中位间隔（S3 网关活性证据）
"""

from __future__ import annotations

import csv
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import store as store_mod   # 只为对齐链路键/节点名的口径

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
GAP_S = 10.0


def median(vals):
    return statistics.median(vals) if vals else 0


def p95(vals):
    if not vals:
        return 0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * 0.95))]


def pick_session(arg: str | None) -> Path:
    if arg:
        d = Path(arg)
        if not d.exists():
            sys.exit("目录不存在: %s" % d)
        return d
    cands = sorted(p for p in DATA_ROOT.iterdir()
                   if p.is_dir() and "overnight" in p.name and (p / "cis_log.csv").exists())
    if not cands:
        sys.exit("data/ 下没有 *-overnight 会话")
    return cands[-1]


def main() -> None:
    sdir = pick_session(sys.argv[1] if len(sys.argv) > 1 else None)
    csv_path = sdir / "cis_log.csv"

    kinds = defaultdict(int)
    links = defaultdict(lambda: {"ts": [], "mean": [], "var": []})
    hourly = defaultdict(lambda: defaultdict(list))   # hour -> link -> [var]
    events = []
    heartbeats = []
    rows = 0
    first_ts = last_ts = None

    with open(csv_path, "r", encoding="utf-8") as fh:
        rd = csv.reader(fh)
        header = next(rd)
        assert header == store_mod.COLUMNS, "表头不符: %s" % header
        for row in rd:
            rows += 1
            try:
                ts = datetime.fromisoformat(row[0]).timestamp()
            except ValueError:
                continue
            first_ts = ts if first_ts is None else first_ts
            last_ts = ts
            kind = row[4]
            kinds[kind] += 1
            if kind == "FEATURES":
                link = "%s>%s" % (row[1], row[2])
                try:
                    var = int(row[8])
                except ValueError:
                    continue
                links[link]["ts"].append(ts)
                links[link]["var"].append(var)
                try:
                    links[link]["mean"].append(int(row[7]))
                except ValueError:
                    pass
                hour = datetime.fromtimestamp(ts).strftime("%H")
                hourly[hour][link].append(var)
            elif kind == "EVENT":
                events.append(row)
            elif kind == "HEARTBEAT":
                heartbeats.append(ts)

    span_s = (last_ts - first_ts) if (first_ts and last_ts) else 0
    dur = "%dmin" % (span_s / 60) if span_s < 7200 else "%.1fh" % (span_s / 3600)

    L = []
    A = L.append
    A("# 夜间无人静置报告 —— %s" % sdir.name)
    A("")
    A("- 会话目录：`%s`" % sdir)
    A("- 生成时间：%s" % datetime.now().isoformat(timespec="seconds"))
    A("- 录制时长：**%s**（首帧 %s ～ 末帧 %s）" % (
        dur,
        datetime.fromtimestamp(first_ts).strftime("%H:%M:%S") if first_ts else "?",
        datetime.fromtimestamp(last_ts).strftime("%H:%M:%S") if last_ts else "?"))
    A("- 总行数：**%d**（%s）" % (
        rows, "、".join("%s=%d" % kv for kv in sorted(kinds.items()))))
    A("- **EVENT 误报数：%d**（验收用例①：空场静置，事件表应为空）" % len(events))
    hb_gaps = [b - a for a, b in zip(heartbeats, heartbeats[1:])]
    A("- HEARTBEAT：%d 条，中位间隔 %.2fs（网关活性）" % (
        len(heartbeats), median(hb_gaps) if hb_gaps else 0))

    status = sdir / "status.log"
    if status.exists():
        lines = status.read_text(encoding="utf-8").strip().splitlines()
        rec = [x for x in lines if "STOPPED" in x or x.startswith("20")]
        A("- status.log：共 %d 行（进程心跳留痕）" % len(rec))
    summ = sdir / "summary.log"
    reconnects = 0
    if summ.exists():
        for line in summ.read_text(encoding="utf-8").splitlines():
            if "重连=" in line:
                try:
                    reconnects = int(line.split("重连=")[1].split()[0])
                except (IndexError, ValueError):
                    pass
    A("- 串口重连次数：**%d**" % reconnects)

    A("")
    A("## 断档（帧间隔 > %.0fs）" % GAP_S)
    gaps_all = []
    for link, d in sorted(links.items()):
        for a, b in zip(d["ts"], d["ts"][1:]):
            if b - a > GAP_S:
                gaps_all.append((b - a, link, a))
    if gaps_all:
        for g, link, at in sorted(gaps_all, reverse=True)[:20]:
            A("- %.0fs @ %s（%s）" % (g, datetime.fromtimestamp(at).strftime("%H:%M:%S"), link))
    else:
        A("- 无 —— 全程链路无 >%ds 空洞" % GAP_S)

    A("")
    A("## 每链路统计（FEATURES 帧）")
    A("")
    A("| 链路 | 行数 | 速率/s | var中位 | var p95 | var max | 尖峰(>4×中位) |")
    A("|---|---|---|---|---|---|---|")
    row_counts = []
    for link, d in sorted(links.items()):
        n = len(d["ts"])
        row_counts.append(n)
        v = d["var"]
        med = median(v)
        spikes = sum(1 for x in v if x > 4 * med) if med else 0
        rate = n / span_s if span_s else 0
        A("| %s | %d | %.1f | %d | %d | %d | %d |" % (
            link, n, rate, med, p95(v), max(v) if v else 0, spikes))
    if row_counts and len(row_counts) == 12:
        spread = (max(row_counts) - min(row_counts)) / median(row_counts) * 100
        A("")
        A("12 链路行数离散度：**%.2f%%**（max-min)/中位）" % spread)

    A("")
    A("## 每小时噪声底（各链路 var 中位数）")
    A("")
    hours = sorted(hourly)
    if hours:
        link_names = sorted({l for h in hours for l in hourly[h]})
        A("| 小时 | %s |" % " | ".join(link_names))
        A("|---|%s" % "---|" * len(link_names))
        for h in hours:
            vals = [median(hourly[h].get(l, [])) for l in link_names]
            A("| %s | %s |" % (h, " | ".join("%d" % v for v in vals)))

    if events:
        A("")
        A("## EVENT 明细（误报警示）")
        A("")
        for r in events:
            A("- %s %s>%s state=%s score=%s rssi=%s" % (r[0], r[1], r[2], r[5], r[6], r[10]))

    out = sdir / "夜间静置报告.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("报告已生成: %s" % out)
    print("\n".join(L[:30]))


if __name__ == "__main__":
    main()
