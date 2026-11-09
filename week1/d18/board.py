"""D19：告警生命周期看板。把 state 里 ALERT 设备的 since / acknowledged /
escalation_level 渲染成一张「当前谁在处理、升到第几级、卡了多久」的总览表。

纯读、不写：看板不碰状态文件，只把现状讲清楚。
既可 CLI 直接打印，也可被 ack_server 调用来渲染网页（render_html）。

    python board.py --state out/alert_state.json --groups groups.json
    python board.py --state out/alert_state.json --html
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from state import load_groups, load_state


def _age_text(stamp: str | None, now: datetime) -> str:
    if not stamp:
        return "-"
    try:
        moment = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return "-"
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    mins = (now - moment).total_seconds() / 60
    if mins < 1:
        return "<1m"
    if mins < 60:
        return f"{mins:.0f}m"
    return f"{mins / 60:.1f}h"


def collect(state: dict, groups: dict, now: datetime) -> tuple[list[dict], list[dict], dict]:
    """返回 (设备行, 组行, 汇总)。只看 ALERT，其余不进看板。"""
    label_to_group: dict[str, str] = {}
    for g in groups.get("groups", []):
        for member in g.get("members", []):
            label_to_group[member] = g["name"]

    device_rows: list[dict] = []
    for label, dev in sorted(state.get("devices", {}).items()):
        if not isinstance(dev, dict) or dev.get("status") != "ALERT":
            continue
        device_rows.append({
            "label": label,
            "since": dev.get("since"),
            "age": _age_text(dev.get("since"), now),
            "stale": bool(dev.get("stale")),
            "acknowledged": bool(dev.get("acknowledged")),
            "acked_by": dev.get("acked_by") or "-",
            "escalation_level": int(dev.get("escalation_level", 0) or 0),
            "group": label_to_group.get(label, "-"),
        })

    group_rows: list[dict] = []
    for g in groups.get("groups", []):
        name = g["name"]
        gstate = state.get("groups", {}).get(name)
        if not gstate or gstate.get("status") != "ALERT":
            continue
        members = set(g.get("members", []))
        alerted = sum(
            1 for m in members
            if state.get("devices", {}).get(m, {}).get("status") == "ALERT"
        )
        group_rows.append({
            "name": name,
            "since": gstate.get("since"),
            "age": _age_text(gstate.get("since"), now),
            "alerted": alerted,
            "total": len(members),
            "ratio": (alerted / len(members)) if members else 0.0,
        })

    acked = sum(1 for r in device_rows if r["acknowledged"])
    escalated = sum(1 for r in device_rows if r["escalation_level"] > 0)
    summary = {
        "alerts": len(device_rows),
        "acked": acked,
        "unacked": len(device_rows) - acked,
        "escalated": escalated,
        "groups_alerting": len(group_rows),
    }
    return device_rows, group_rows, summary


def render_text(device_rows: list[dict], group_rows: list[dict], summary: dict) -> str:
    lines: list[str] = []
    lines.append("=== 告警生命周期看板 ===")
    lines.append(
        f"当前 ALERT {summary['alerts']} 台 ｜ 已确认 {summary['acked']} ｜ "
        f"未确认 {summary['unacked']} ｜ 已升级 {summary['escalated']} ｜ "
        f"故障组 {summary['groups_alerting']}"
    )
    if group_rows:
        lines.append("")
        lines.append(f"{'组':<10}{'状态':<8}{'占比':<10}{'持续':<8}")
        for g in group_rows:
            lines.append(f"{g['name']:<10}{'ALERT':<8}{g['alerted']}/{g['total']:<8}{g['age']:<8}")
    if device_rows:
        lines.append("")
        header = f"{'设备':<22}{'持续':<8}{'级别':<6}{'确认人':<12}{'组':<10}{'失联':<6}"
        lines.append(header)
        for r in device_rows:
            lvl = f"L{r['escalation_level']}" if r["escalation_level"] else "-"
            ack = r["acked_by"] if r["acknowledged"] else "未确认"
            stale = "是" if r["stale"] else ""
            lines.append(
                f"{r['label']:<22}{r['age']:<8}{lvl:<6}{ack:<12}{r['group']:<10}{stale:<6}"
            )
    else:
        lines.append("（当前没有 ALERT 设备）")
    return "\n".join(lines)


def render_html(device_rows: list[dict], group_rows: list[dict], summary: dict) -> str:
    def esc(s) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts: list[str] = ["<html><head><meta charset='utf-8'><title>告警看板</title></head><body>"]
    parts.append("<h1>告警生命周期看板</h1>")
    parts.append(
        f"<p>ALERT {summary['alerts']} ｜ 已确认 {summary['acked']} ｜ "
        f"未确认 {summary['unacked']} ｜ 已升级 {summary['escalated']} ｜ "
        f"故障组 {summary['groups_alerting']}</p>"
    )
    if group_rows:
        parts.append("<h2>故障组</h2><table border='1' cellpadding='4'>")
        parts.append("<tr><th>组</th><th>占比</th><th>持续</th></tr>")
        for g in group_rows:
            parts.append(
                f"<tr><td>{esc(g['name'])}</td><td>{g['alerted']}/{g['total']} "
                f"({g['ratio'] * 100:.0f}%)</td><td>{esc(g['age'])}</td></tr>"
            )
        parts.append("</table>")
    parts.append("<h2>设备</h2><table border='1' cellpadding='4'>")
    parts.append("<tr><th>设备</th><th>持续</th><th>级别</th><th>确认人</th><th>组</th><th>失联</th></tr>")
    for r in device_rows:
        lvl = f"L{r['escalation_level']}" if r["escalation_level"] else "-"
        ack = r["acked_by"] if r["acknowledged"] else "未确认"
        parts.append(
            f"<tr><td>{esc(r['label'])}</td><td>{esc(r['age'])}</td><td>{esc(lvl)}</td>"
            f"<td>{esc(ack)}</td><td>{esc(r['group'])}</td><td>{'是' if r['stale'] else ''}</td></tr>"
        )
    parts.append("</table></body></html>")
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="D19：告警生命周期看板")
    parser.add_argument("--state", default="out/alert_state.json", help="告警状态文件")
    parser.add_argument("--groups", help="分组拓扑 JSON")
    parser.add_argument("--html", action="store_true", help="输出 HTML 而非文本")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    now = datetime.now()
    state, _ = load_state(args.state, now=now)
    groups = load_groups(args.groups)
    device_rows, group_rows, summary = collect(state, groups, now)

    if args.json:
        print(json.dumps({"devices": device_rows, "groups": group_rows, "summary": summary},
                         ensure_ascii=False, indent=2))
    elif args.html:
        print(render_html(device_rows, group_rows, summary))
    else:
        print(render_text(device_rows, group_rows, summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
