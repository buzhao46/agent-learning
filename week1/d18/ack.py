"""D18：人工确认告警（ack）。

    python ack.py --state out/alert_state.json --device 10.0.0.9:161 --by alice
    python ack.py --state out/alert_state.json --group 机房A --by bob

只改状态里 ALERT 设备的 acknowledged 字段，不影响状态机跃迁。下次 alert.py
跑的时候，被确认的设备不会再触发升级（maybe_escalate 会跳过它）。

退出码：0 成功，2 状态文件不存在/损坏。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from state import load_groups, load_state, save_state


def main() -> int:
    parser = argparse.ArgumentParser(description="D18：人工确认告警")
    parser.add_argument("--state", required=True, help="告警状态文件")
    parser.add_argument("--device", help="单台设备 label，如 10.0.0.9:161")
    parser.add_argument("--group", help="按组名批量确认")
    parser.add_argument("--by", help="确认人")
    parser.add_argument("--note", help="备注")
    parser.add_argument("--groups", help="分组拓扑 JSON（--group 展开用）")
    args = parser.parse_args()

    from pathlib import Path

    if not Path(args.state).exists():
        print(f"状态文件不存在：{args.state}")
        return 2

    now = datetime.now()
    state, _ = load_state(args.state, now=now)

    targets: set[str] = set()
    if args.device:
        targets.add(args.device)
    if args.group:
        groups = load_groups(args.groups)
        members = [m for g in groups.get("groups", []) if g.get("name") == args.group for m in g.get("members", [])]
        if not members:
            print(f"[警告] 分组 {args.group!r} 里没有设备")
        targets.update(members)

    if not targets:
        print("必须指定 --device 或 --group")
        return 2

    acked = 0
    for label in sorted(targets):
        dev = state.get("devices", {}).get(label)
        if not isinstance(dev, dict):
            print(f"[跳过] {label} 不在状态中")
            continue
        if dev.get("status") != "ALERT":
            print(f"[跳过] {label} 不是 ALERT（{dev.get('status')}）")
            continue
        dev["acknowledged"] = True
        dev["acked_by"] = args.by or "unknown"
        dev["acked_at"] = now.isoformat(timespec="seconds")
        dev["ack_note"] = args.note or ""
        acked += 1
        print(f"[确认] {label} by {dev['acked_by']}")

    save_state(state, args.state, now=now)
    print(f"已确认 {acked} 台")
    return 0


if __name__ == "__main__":
    sys.exit(main())
