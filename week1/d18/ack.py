"""D18→D19：人工确认告警（ack）。

    python ack.py --state out/alert_state.json --device 10.0.0.9:161 --by alice
    python ack.py --state out/alert_state.json --group 机房A --by bob

只改状态里 ALERT 设备的 acknowledged 字段，不影响状态机跃迁。下次 alert.py
跑的时候，被确认的设备不会再触发升级（maybe_escalate 会跳过它）。

D19 把「算目标 / 写确认」抽成 resolve_targets / apply_ack 两个纯函数，
ack_server.py 的 HTTP 回调直接复用，CLI 只是它们的一个薄封装。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from state import load_groups, load_state, save_state


def resolve_targets(
    state: dict, groups_path: str | None, device: str | None, group: str | None
) -> set[str]:
    """把 --device / --group 解析成要确认的设备 label 集合。"""
    targets: set[str] = set()
    if device:
        targets.add(device)
    if group:
        groups = load_groups(groups_path)
        members = [
            m
            for g in groups.get("groups", [])
            if g.get("name") == group
            for m in g.get("members", [])
        ]
        if not members:
            print(f"[警告] 分组 {group!r} 里没有设备")
        targets.update(members)
    return targets


def apply_ack(
    state: dict, targets: set[str], by: str | None, note: str | None, now: datetime
) -> int:
    """对 targets 里仍是 ALERT 的设备写确认。返回确认了几台。直接改 state。"""
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
        dev["acked_by"] = by or "unknown"
        dev["acked_at"] = now.isoformat(timespec="seconds")
        dev["ack_note"] = note or ""
        acked += 1
        print(f"[确认] {label} by {dev['acked_by']}")
    return acked


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

    targets = resolve_targets(state, args.groups, args.device, args.group)
    if not targets:
        print("必须指定 --device 或 --group")
        return 2

    apply_ack(state, targets, args.by, args.note, now)
    save_state(state, args.state, now=now)
    print(f"已确认 {len(targets)} 个目标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
