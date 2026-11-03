"""D16：多份巡检报告 → 设备趋势 + 降噪告警。

    python trend.py --report-dir out --threshold 3 [--json out/trend.json]

为什么不塞进 probe.py：
    probe 负责「采」，trend 负责「算」，两者的失败域不一样 ——
    采集失败是一台设备的问题，趋势算错是所有设备一起骗人。
    分开之后 trend.py 不需要 SNMP 库，验收可以直接构造 JSON 喂它。

退出码（沿用 D15 的三态惯例，含义写清楚，自动化才敢接）：
    0  全部 OK            不动作
    1  存在 ALERT          发通知
    2  存在 UNKNOWN        不发通知，先补数据
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from history import (
    STATUS_ALERT,
    STATUS_UNKNOWN,
    build_trends,
    trend_to_dict,
)
from ifwalk import disp_width, pad
from report import write_json_atomic

HEADERS = ["设备", "样本", "最近", "连续非OK", "uptime变化", "接口数", "状态", "原因"]


def render_table(trends: list) -> None:
    """终端表格。用 disp_width/pad 而不是 ljust：中文占两格，len() 对齐必然错位。"""
    rows = [
        [
            t.label,
            str(t.samples),
            t.last_verdict,
            str(t.consecutive_bad),
            t.fmt_delta(),
            "-" if t.if_count is None else str(t.if_count),
            t.status,
            t.reason,
        ]
        for t in trends
    ]
    widths = [max([disp_width(h)] + [disp_width(r[i]) for r in rows]) for i, h in enumerate(HEADERS)]

    print("  ".join(pad(h, widths[i]) for i, h in enumerate(HEADERS)).rstrip())
    print("  ".join("-" * widths[i] for i in range(len(HEADERS))))
    for row in rows:
        print("  ".join(pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip())


def summarize(trends: list) -> dict:
    """状态计数。json 和终端共用一份，避免两处各算一次算出不同结果。"""
    return {
        "total": len(trends),
        "alert": sum(1 for t in trends if t.status == STATUS_ALERT),
        "ok": sum(1 for t in trends if t.status == "OK"),
        "unknown": sum(1 for t in trends if t.status == STATUS_UNKNOWN),
    }


def exit_code(trends: list) -> int:
    if any(t.status == STATUS_ALERT for t in trends):
        return 1
    if any(t.status == STATUS_UNKNOWN for t in trends):
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="多份巡检报告的设备趋势与降噪告警")
    parser.add_argument("--report-dir", default="out", help="scan_*.json 所在目录")
    parser.add_argument("--threshold", type=int, default=3, help="连续 N 次非 OK 才告警")
    parser.add_argument("--json", dest="json_path", help="趋势结果另存一份 JSON")
    args = parser.parse_args()

    if args.threshold < 1:
        print("--threshold 必须 >= 1")
        return 2

    try:
        trends, groups = build_trends(args.report_dir, args.threshold)
    except OSError as exc:
        print(f"读取报告失败：{exc}")
        return 2

    if not trends:
        print(f"{args.report_dir} 下没有可用报告（先跑一次 probe.py --config）")
        return 2

    print(f"\n趋势：{args.report_dir}　阈值 {args.threshold} 次　{len(groups)} 台设备\n")
    render_table(trends)

    summary = summarize(trends)
    print(
        f"\n合计：{summary['total']} 台 → "
        f"ALERT {summary['alert']} / OK {summary['ok']} / UNKNOWN {summary['unknown']}"
    )

    if args.json_path:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "report_dir": args.report_dir,
            "threshold": args.threshold,
            "summary": summary,
            "trends": [trend_to_dict(t) for t in trends],
        }
        path = write_json_atomic(Path(args.json_path), payload)
        print(f"趋势已写入 {path}")

    return exit_code(trends)


if __name__ == "__main__":
    sys.exit(main())
