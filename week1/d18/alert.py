"""D18：告警状态机入口 —— 在 D17 基础上加升级扫描与组收敛。

    python alert.py --report-dir out --state out/alert_state.json --channel file
    python alert.py --report-dir out --groups groups.json --escalate-webhook http://...
    python alert.py --report-dir out --dry-run

退出码（和 D17 一致）：
    0  本次没有新事件
    1  产生了事件（含 dry-run）
    2  状态文件读写失败，或有待发事件发送失败

D18 在 D17 五步流程里插了两步：
    3.5  升级扫描：对每台 ALERT 且未 ack 的设备，超时则升级（独立的 maybe_escalate）
    4.5  收敛：同组设备批量故障合并成一条 group_alert，抑制单设备 open

两个原则（详见 README）：
    - 收敛只抑制「通知」，不抑制「状态」；设备各自还是 ALERT。
    - 升级事件与组告警走独立渠道（--escalate-channel / --escalate-webhook）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import acktoken
import roster
from alerts import (
    EVENT_DIGEST,
    EVENT_ESC_DIGEST,
    EVENT_ESCALATE,
    EVENT_GROUP,
    EVENT_OPEN,
    Event,
    Observation,
    is_silenced,
    maybe_escalate,
    observe,
    step,
)
from history import build_trends
from notify import send
from state import enqueue, load_groups, load_state, save_state


def channel_for(kind: str, args) -> tuple[str, str | None]:
    """升级事件、组告警、升级摘要走升级渠道，其余（含新告警摘要）走普通渠道。"""
    if kind in (EVENT_ESCALATE, EVENT_GROUP, EVENT_ESC_DIGEST):
        return args.escalate_channel, args.escalate_webhook
    return args.channel, args.webhook


def build_digest(events: list, state: dict, args, now: datetime, kind: str) -> dict:
    """把同一轮的一批同类型事件聚成一条摘要。

    kind == EVENT_DIGEST   本轮新增的 alert_open 批量（散装故障，没进组收敛的）
    kind == EVENT_ESC_DIGEST  本轮升级事件批量

    每条 items 带各自的确认链接；摘要只发一条，省掉「20 台同挂 → 20 条消息」。
    组收敛（group_alert）是另一回事：它基于拓扑、只管已知组；digest 基于「同一轮」、
    管任意一批同时发生的告警，两者互补，不冲突。
    """
    is_escalate = kind == EVENT_ESC_DIGEST
    stamp = now.isoformat(timespec="seconds")
    on_duty_person = roster.on_duty(args.roster, now, args.shift) if args.roster else None
    items: list[dict] = []
    for ev in events:
        dev = state.get("devices", {}).get(ev.label, {})
        item = {
            "device": ev.label,
            "ack_url": ack_url_for(EVENT_OPEN, ev.label, args, now),
            "reason": ev.reason,
        }
        if is_escalate:
            item["level"] = int(dev.get("escalation_level", 0) or 0)
            since = dev.get("since")
            waited = None
            if since:
                try:
                    moment = datetime.fromisoformat(str(since))
                    if moment.tzinfo is not None:
                        moment = moment.astimezone().replace(tzinfo=None)
                    waited = (now - moment).total_seconds() / 60
                except (TypeError, ValueError):
                    waited = None
            item["waited_minutes"] = round(waited) if waited is not None else None
        items.append(item)

    if is_escalate:
        reason = f"本轮升级 {len(items)} 台（已聚合，避免刷屏）"
        if on_duty_person:
            reason += f"；@值班人 {on_duty_person}"
    else:
        reason = f"本轮新增 {len(items)} 台"

    return {
        "id": f"{kind}:digest:{stamp}",
        "kind": kind,
        "device": "digest",
        "old_status": None,
        "new_status": "ALERT",
        "reason": reason,
        "created_at": stamp,
        "items": items,
        "suppressed": False,
        "attempts": 0,
    }


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


def correlate(
    events: list,
    state: dict,
    groups: dict,
    label_to_group: dict,
    threshold: float,
    now: datetime,
) -> tuple[list, dict]:
    """组收敛：同组批量故障合并成一条 group_alert，抑制单设备 open。

    返回 (最终要发的事件, 更新后的 state)。

    - 统计每个组当前 ALERT 数 / 组内总数
    - 越过阈值的组：单设备 alert_open 被丢弃（continue），生成/复用一条 group_alert
    - 组恢复（低于阈值且原是 ALERT）：发一条恢复 group_alert，组状态清回 OK
    - group 状态存 state["groups"]，绝不碰 state["devices"]
    """
    group_stats: dict[str, tuple[int, int]] = {}
    for g in groups.get("groups", []):
        members = set(g.get("members", []))
        alerted = [m for m in members if state["devices"].get(m, {}).get("status") == "ALERT"]
        group_stats[g["name"]] = (len(alerted), len(members))

    stamp = now.isoformat(timespec="seconds")
    final_events = []
    for ev in events:
        gname = label_to_group.get(ev.label)
        if gname:
            n_alert, n_total = group_stats[gname]
            if n_total and n_alert / n_total >= threshold and ev.kind == EVENT_OPEN:
                continue  # 被收敛，不单独发
        final_events.append(ev)

    for g in groups.get("groups", []):
        n_alert, n_total = group_stats[g["name"]]
        gstate = state["groups"].setdefault(g["name"], {"status": "OK", "since": None})
        if n_total and n_alert / n_total >= threshold:
            if gstate["status"] != "ALERT":
                gstate["status"] = "ALERT"
                gstate["since"] = stamp
                final_events.append(
                    Event(
                        EVENT_GROUP,
                        g["name"],
                        "OK",
                        "ALERT",
                        f"{g['name']}：{n_alert}/{n_total} 设备不可达，疑似上游故障",
                        stamp,
                    )
                )
        else:
            if gstate["status"] == "ALERT":
                final_events.append(
                    Event(
                        EVENT_GROUP,
                        g["name"],
                        "ALERT",
                        "OK",
                        f"{g['name']} 故障恢复，剩 {n_alert}/{n_total} 台异常",
                        stamp,
                    )
                )
            gstate["status"] = "OK"
    return final_events, state


def dispatch(event: dict, args, silences: list[dict], now: datetime) -> tuple[bool, bool]:
    """发一条事件。返回 (需要入队?, 发送失败?)。"""
    channel, webhook = channel_for(event["kind"], args)
    silenced, why = is_silenced(event["device"], now, silences)
    if silenced:
        print(f"[静默] {event['kind']:<11} {event['device']:<20} {why}")
        return False, False

    ok, message = send(
        event,
        channel=channel,
        webhook=webhook,
        log_path=args.log,
    )
    if ok:
        print(f"[发送] {event['kind']:<11} {event['device']:<20} → {message}")
        return False, False

    print(f"[发送失败→入队] {event['kind']:<11} {event['device']:<20} → {message}")
    return True, True


def ack_url_for(kind: str, label: str, args, now: datetime) -> str | None:
    """给一条事件算确认链接。没配 --ack-base-url 就不带链接。

    D21：若配了排班表（--roster）并能查到当前值班人，则把 by= 预填进链接，
    点链接的人默认就是当班人，不用手填。by= 不参与签名（只是「谁点的」的预填）。
    """
    if not args.ack_base_url:
        return None
    by = roster.on_duty(args.roster, now, args.shift) if args.roster else None
    if kind == EVENT_GROUP:
        return acktoken.group_ack_url(args.ack_base_url, label, args.ack_secret, by=by)
    return acktoken.device_ack_url(args.ack_base_url, label, args.ack_secret, by=by)


def main() -> int:
    parser = argparse.ArgumentParser(description="D18：告警状态机（含升级与收敛）")
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
    parser.add_argument("--groups", help="分组拓扑 JSON（机房/网段 → 设备清单）")
    parser.add_argument(
        "--group-threshold", type=float, default=0.6, help="组内 ALERT 占比超过此值才收敛"
    )
    parser.add_argument("--escalate-after", type=int, default=30, help="未确认多少分钟升一级")
    parser.add_argument("--max-level", type=int, default=2, help="升级最高级别")
    parser.add_argument(
        "--escalate-channel", default="webhook", choices=["console", "file", "webhook"]
    )
    parser.add_argument("--escalate-webhook", help="升级事件的目标 URL")
    parser.add_argument("--ack-base-url", help="确认链接基地址，如 http://host:8731（D19 闭环）")
    parser.add_argument("--ack-secret", default=acktoken.DEFAULT_SECRET, help="ack 链接签名密钥")
    parser.add_argument(
        "--roster", help="值班排班表 JSON（D21）：日期+班次→值班人，默认填到 ack 链接 by="
    )
    parser.add_argument("--shift", help="强制指定班次（day/night），不传则按当前小时推断")
    parser.add_argument(
        "--digest-threshold", type=int, default=2, help="同轮同类型事件≥N 条才聚成摘要（默认 2）"
    )
    parser.add_argument("--no-digest", action="store_true", help="关闭摘要聚合，每条事件单独发")
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

    # 1. 算趋势 → 过状态机
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

    # 2. 升级扫描（独立纯函数，不污染 step 的跃迁语义）
    escalation_events = []
    for label, dev in state["devices"].items():
        new_dev, event = maybe_escalate(
            label,
            dev,
            escalate_after=args.escalate_after,
            max_level=args.max_level,
            now=now,
        )
        state["devices"][label] = new_dev
        if event is not None:
            escalation_events.append(event)
            print(f"  [升级] {label} → L{new_dev['escalation_level']}")

    all_events = new_events + escalation_events

    # 3. 收敛（只动 state["groups"]，不碰 state["devices"]）
    groups = load_groups(args.groups)
    label_to_group: dict[str, str] = {}
    for g in groups.get("groups", []):
        for member in g.get("members", []):
            label_to_group[member] = g["name"]
    all_events, state = correlate(
        all_events, state, groups, label_to_group, args.group_threshold, now
    )

    if args.dry_run:
        print(f"\n[dry-run] 将产生 {len(all_events)} 条事件，状态不写盘、通知不发送")
        for event in all_events:
            print(f"  [将发] {event.kind:<14} {event.label:<20} {event.reason}")
        return 1 if all_events else 0

    silences = load_silences(args.silences, args.silence_until)
    failures = 0

    # 4. 先补发上次没送出去的（按事件 kind 选渠道）
    remaining = []
    for event in state.get("pending", []):
        url = ack_url_for(event.get("kind", ""), event.get("device", ""), args, now)
        if url:
            event["ack_url"] = url
        channel, webhook = channel_for(event.get("kind", ""), args)
        ok, message = send(event, channel=channel, webhook=webhook, log_path=args.log)
        if ok:
            print(f"[补发] {event.get('id')} → {message}")
        else:
            event["attempts"] = int(event.get("attempts", 0) or 0) + 1
            remaining.append(event)
            failures += 1
            print(f"[补发失败] {event.get('id')} → {message}")
    state["pending"] = remaining

    # 5. 新事件：先按「同轮同类型」聚合成摘要（降噪），再静默过滤后发送
    opens = [ev for ev in all_events if ev.kind == EVENT_OPEN]
    escalations = [ev for ev in all_events if ev.kind == EVENT_ESCALATE]
    others = [ev for ev in all_events if ev.kind not in (EVENT_OPEN, EVENT_ESCALATE)]

    # 静默的处理保留在 dispatch 里（会打印 [静默] 且不发、不入队）。
    # 这里只把「未静默」的同类型事件挑出来聚合成摘要；被静默的事件仍单独走 dispatch，
    # 既保证 [静默] 提示不丢，又保证摘要里不会出现被静默的设备。
    active_opens: list = []
    active_escalations: list = []
    to_send: list = list(others)
    for ev in opens:
        silenced, _ = is_silenced(ev.label, now, silences)
        if silenced:
            to_send.append(ev)  # 走 dispatch：打印 [静默]，不发、不入队
        else:
            active_opens.append(ev)
    for ev in escalations:
        silenced, _ = is_silenced(ev.label, now, silences)
        if silenced:
            to_send.append(ev)
        else:
            active_escalations.append(ev)

    if not args.no_digest and len(active_opens) >= args.digest_threshold:
        to_send.append(build_digest(active_opens, state, args, now, EVENT_DIGEST))
        print(f"  [聚合] {len(active_opens)} 条新告警 → 1 条摘要（阈值 {args.digest_threshold}）")
    else:
        to_send.extend(active_opens)
    if not args.no_digest and len(active_escalations) >= args.digest_threshold:
        to_send.append(build_digest(active_escalations, state, args, now, EVENT_ESC_DIGEST))
        print(
            f"  [聚合] {len(active_escalations)} 条升级 → 1 条升级摘要（阈值 {args.digest_threshold}）"
        )
    else:
        to_send.extend(active_escalations)

    for event in to_send:
        payload = event.to_dict() if isinstance(event, Event) else event
        if payload.get("kind") in (EVENT_DIGEST, EVENT_ESC_DIGEST):
            url = None  # 摘要里每条 items 自带 ack_url，顶层不再加
        else:
            url = ack_url_for(payload.get("kind", ""), payload.get("device", ""), args, now)
        if url:
            payload["ack_url"] = url
        need_queue, failed = dispatch(payload, args, silences, now)
        if need_queue:
            enqueue(state, payload)
        if failed:
            failures += 1

    # 6. 状态只前进，不因投递失败回滚
    try:
        path = save_state(state, args.state, now=now)
    except OSError as exc:
        print(f"写状态文件失败：{exc}")
        return 2
    print(f"状态已写入 {path}（待发 {len(state['pending'])} 条，组状态 {len(state['groups'])} 个）")

    if failures:
        return 2
    return 1 if all_events else 0


if __name__ == "__main__":
    sys.exit(main())
