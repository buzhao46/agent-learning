"""D17：告警状态机入口 —— 把 D16 的趋势变成「只在状态变化时发一次」的事件。

    python alert.py --report-dir out --state out/alert_state.json --channel file
    python alert.py --report-dir out --dry-run          # 只看会发什么，零副作用

退出码：
    0  本次没有新事件
    1  产生了事件（含 dry-run）
    2  状态文件读写失败，或有待发事件发送失败

⚠️ 0 的含义不是「设备都好」，只是「没有新事件」。设备昨天就坏了、今天一直没好，
   退出码同样是 0。把它当健康检查接进监控会出事 —— 要判健康请看 trend.py 的退出码。

流程固定五步，顺序不能换：
    1. 读状态（坏文件先备份，按首次运行处理）
    2. 算趋势（直接 import history，不读 trend.py 的 JSON：少一跳、少一个失败点）
    3. 补发 outbox 里上次没送出去的事件
    4. 每台设备过状态机 → 新事件 → 静默过滤 → 发送 / 入队
    5. 原子写回状态
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from alerts import Observation, is_silenced, observe, step
from history import build_trends
from notify import send
from state import enqueue, load_state, save_state


def load_silences(path: str | None, silence_until: str | None) -> list[dict]:
    """读静默配置：文件里的列表 + 命令行临时全局静默。"""
    silences: list[dict] = []
    if path:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[警告] 静默文件读取失败，忽略：{exc}")
            data = []
        if isinstance(data, list):
            silences.extend(item for item in data if isinstance(item, dict))
        else:
            print("[警告] 静默文件应是 JSON 数组，已忽略")

    if silence_until:
        silences.append({"device": "*", "until": silence_until, "reason": "命令行静默"})
    return silences


def collect_observations(trends, state: dict) -> list[Observation]:
    """本次趋势 + 状态里已有但本次没出现的设备。

    第二半很关键：增量巡检会跳过上次 OK 的设备，这些设备在本次报告里根本不存在。
    如果只按 trends 迭代，它们会从状态机里消失，既不会 stale，也不会告警 ——
    监控自己瞎了，却报平安。
    """
    observations = [observe(trend) for trend in trends]
    known = {obs.label for obs in observations}
    for label, prev in state.get("devices", {}).items():
        if label in known or not isinstance(prev, dict):
            continue
        observations.append(
            Observation(
                label=label,
                verdict="UNKNOWN",
                consecutive_bad=int(prev.get("consecutive_bad", 0) or 0),
                consecutive_ok=int(prev.get("consecutive_ok", 0) or 0),
                last_seen=prev.get("last_seen"),
            )
        )
        print(f"[提示] {label} 本次报告里没有，按「无数据」处理（状态保持 {prev.get('status')}）")
    observations.sort(key=lambda obs: obs.label)
    return observations


def dispatch(event: dict, args, silences: list[dict], now: datetime) -> tuple[bool, bool]:
    """发一条事件。返回 (需要入队?, 发送失败?)。"""
    silenced, why = is_silenced(event["device"], now, silences)
    if silenced:
        print(f"[静默] {event['kind']:<11} {event['device']:<20} {why}")
        return False, False

    ok, message = send(
        event,
        channel=args.channel,
        webhook=args.webhook,
        log_path=args.log,
    )
    if ok:
        print(f"[发送] {event['kind']:<11} {event['device']:<20} → {message}")
        return False, False

    print(f"[发送失败→入队] {event['kind']:<11} {event['device']:<20} → {message}")
    return True, True


def main() -> int:
    parser = argparse.ArgumentParser(description="D17：告警状态机与通知")
    parser.add_argument("--report-dir", default="out", help="scan_*.json 所在目录")
    parser.add_argument("--state", default="out/alert_state.json", help="告警状态文件")
    parser.add_argument("--threshold", type=int, default=3, help="连续 N 次非 OK 才进入 ALERT")
    parser.add_argument("--recover", type=int, default=2, help="连续 N 次 OK 才消警（防抖）")
    parser.add_argument("--stale-after", type=int, default=30, help="多少分钟没新数据算过期")
    parser.add_argument("--channel", default="console", choices=["console", "file", "webhook"])
    parser.add_argument("--webhook", help="channel=webhook 时的目标 URL")
    parser.add_argument("--log", default="out/alerts.log", help="channel=file 时的日志路径")
    parser.add_argument("--silences", help="静默配置 JSON（数组）")
    parser.add_argument("--silence-until", help="全局静默到某个时间点（ISO）")
    parser.add_argument("--dry-run", action="store_true", help="只算不写：不改状态、不发通知")
    args = parser.parse_args()

    if args.threshold < 1 or args.recover < 1:
        print("--threshold / --recover 必须 >= 1")
        return 2

    now = datetime.now()
    state, note = load_state(args.state, now=now)
    print(f"状态：{note}")

    try:
        trends, _groups = build_trends(args.report_dir, args.threshold)
    except OSError as exc:
        print(f"读取报告失败：{exc}")
        return 2

    if not trends and not state.get("devices"):
        print(f"{args.report_dir} 下没有报告，也没有历史状态，无事可做")
        return 0

    print(
        f"趋势：{len(trends)} 台；状态文件：{len(state.get('devices', {}))} 台；待发 {len(state.get('pending', []))} 条"
    )

    observations = collect_observations(trends, state)
    new_events = []
    for obs in observations:
        prev = state["devices"].get(obs.label)
        new_state, events = step(
            prev,
            obs,
            recover=args.recover,
            stale_after=args.stale_after,
            now=now,
        )
        state["devices"][obs.label] = new_state
        old_status = (prev or {}).get("status", "-")
        marks = ", ".join(event.kind for event in events) or "-"
        stale_mark = " (stale)" if new_state.get("stale") else ""
        print(
            f"  {obs.label:<22} {old_status:<8} → {new_state['status']:<8}{stale_mark:<8} {marks}"
        )
        new_events.extend(events)

    if args.dry_run:
        print(f"\n[dry-run] 将产生 {len(new_events)} 条事件，状态不写盘、通知不发送")
        for event in new_events:
            print(f"  [将发] {event.kind:<11} {event.label:<20} {event.reason}")
        return 1 if new_events else 0

    silences = load_silences(args.silences, args.silence_until)
    failures = 0

    # 3. 先补发上次没送出去的
    remaining = []
    for event in state.get("pending", []):
        ok, message = send(event, channel=args.channel, webhook=args.webhook, log_path=args.log)
        if ok:
            print(f"[补发] {event.get('id')} → {message}")
        else:
            event["attempts"] = int(event.get("attempts", 0) or 0) + 1
            remaining.append(event)
            failures += 1
            print(f"[补发失败] {event.get('id')} → {message}")
    state["pending"] = remaining

    # 4. 新事件：静默过滤后发送，发不出去的进 outbox
    for event in new_events:
        payload = event.to_dict()
        need_queue, failed = dispatch(payload, args, silences, now)
        if need_queue:
            enqueue(state, payload)
        if failed:
            failures += 1

    # 5. 状态只前进，不因投递失败回滚
    try:
        path = save_state(state, args.state, now=now)
    except OSError as exc:
        print(f"写状态文件失败：{exc}")
        return 2
    print(f"状态已写入 {path}（待发 {len(state['pending'])} 条）")

    if failures:
        return 2
    return 1 if new_events else 0


if __name__ == "__main__":
    sys.exit(main())
