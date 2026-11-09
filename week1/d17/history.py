"""D16：把 report_dir 下的多份 scan_*.json 聚合成「每台设备的历史」，再算趋势。

职责边界：这里只做**读取 + 聚合 + 计算 + 判定**，不打印、不写文件。
打印在 trend.py，落盘格式在 report.py —— 三者互不越界。

三个关键口径（想不清就会算错）：
  1. 设备身份是 (host, port)。127.0.0.1:57326 和 :57327 是两台设备，
     只按 host 会把两条历史揉成一条。
  2. uptime 必须用原始 ticks 比。报告里的 "0:34:17" 是给人看的，
     字符串之间没法判断「变小了」，也就发现不了重启。
  3. 一坏一好交替时，"连续失败次数"要能从尾部往回数到第一台好设备为止。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

STATUS_ALERT = "ALERT"
STATUS_OK = "OK"
STATUS_UNKNOWN = "UNKNOWN"


def device_key(dev: dict) -> tuple[str, int]:
    """设备身份 = (host, port)。

    与 probe._dev_key 同一口径：两处必须一致，否则聚合时同一个设备会被拆成两组。
    旧报告（D15 早期）没有 port 字段，按 SNMP 默认 161 兜底。
    """
    try:
        port = int(dev.get("port", 161))
    except (TypeError, ValueError):
        port = 161
    return (str(dev.get("host", "")), port)


def load_reports(report_dir: str) -> list[dict]:
    """按时间升序返回目录下所有报告；坏文件跳过并告警。

    排序键用 (generated_at, 文件名)：generated_at 只到秒，同一秒内的两次扫描
    必须靠文件名里的毫秒分先后。一份坏 JSON 不能把整段历史搞崩。
    """
    items: list[tuple[Path, dict]] = []
    for path in sorted(Path(report_dir).glob("scan_*.json")):
        try:
            # utf-8-sig：容忍被记事本 / PowerShell 加过 BOM 的历史文件
            with path.open(encoding="utf-8-sig") as handle:
                items.append((path, json.load(handle)))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[警告] 跳过坏报告 {path.name}：{exc}")
    items.sort(key=lambda it: (it[1].get("generated_at", ""), it[0].name))
    return [report for _, report in items]


def group_by_device(reports: list[dict]) -> dict[tuple[str, int], list[tuple[str, dict]]]:
    """{(host, port): [(generated_at, 设备快照), ...]}，每组内部保持时间升序。

    增量巡检的报告里只含本次重扫的设备，所以必须逐份展开再归组，
    不能假设每份报告都包含全部设备。

    为什么把 generated_at 一起带上：D17 的状态机要判断「这台设备多久没消息了」。
    只看 samples 条数答不了这个问题 —— 一条 5 分钟前的样本和一条 3 小时前的
    样本，条数都是 1。时间戳必须从报告顶层跟着设备片段一起下来。
    """
    groups: dict[tuple[str, int], list[tuple[str, dict]]] = {}
    for report in reports:
        stamp = str(report.get("generated_at", ""))
        for dev in report.get("devices", []):
            groups.setdefault(device_key(dev), []).append((stamp, dev))
    return groups


def _tail_run(verdicts: list[str], match) -> int:
    """从尾部往回数，满足 match 的连续个数。遇到第一个不满足的就停。"""
    count = 0
    for verdict in reversed(verdicts):
        if not match(verdict):
            break
        count += 1
    return count


@dataclass
class Trend:
    """一台设备的历史汇总。字段只放算出来的结论，原始快照不重复存。"""

    host: str
    port: int
    samples: int = 0
    ok: int = 0
    degraded: int = 0
    failed: int = 0
    last_verdict: str = "OK"
    prev_verdict: str | None = None
    consecutive_bad: int = 0
    consecutive_ok: int = 0
    uptime_ticks: int | None = None
    uptime_delta: int | None = None
    restarted: bool = False
    if_count: int | None = None
    if_count_changes: int = 0
    last_seen: str | None = None
    status: str = STATUS_OK
    reason: str = ""

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"

    def fmt_delta(self) -> str:
        """ticks（1/100 秒）→ 人读的秒数。数据不足或期间重启时给 \"-\"。"""
        if self.uptime_delta is None:
            return "-"
        sign = "+" if self.uptime_delta >= 0 else ""
        return f"{sign}{self.uptime_delta / 100:.0f}s"


def analyze(items: list[tuple[str, dict]]) -> Trend:
    """一串按时间升序的 (generated_at, 设备快照) → 趋势。

    计算规则（每条都对应一个真实坑）：
      - consecutive_*：从最新往回数连续段，不是简单计数；
      - 非 OK 包含 DEGRADED：只采到一半的设备运维一样要处理；
      - uptime 缺值（None）直接跳过，不能用 0 兜底 —— 否则「采不到」
        会被算成「刚重启」；
      - 任意相邻两点出现 ticks 变小 → restarted；跨重启的 delta 无意义，
        因此置 None，别给一个 -12340678 的负数让人误读；
      - ifTable 只存接口**个数**，个数变化只能算弱信号，不叫 flapping；
      - last_seen 取最后一条样本的报告时间，D17 靠它判断数据是否过期。
    """
    if not items:
        raise ValueError("items 不能为空")

    samples = [dev for _, dev in items]
    host, port = device_key(samples[0])
    trend = Trend(host=host, port=port, samples=len(samples))
    trend.last_seen = items[-1][0] or None
    verdicts = [str(dev.get("verdict", "FAILED")) for dev in samples]

    trend.ok = verdicts.count("OK")
    trend.degraded = verdicts.count("DEGRADED")
    trend.failed = verdicts.count("FAILED")
    trend.last_verdict = verdicts[-1]
    trend.prev_verdict = verdicts[-2] if len(verdicts) > 1 else None
    trend.consecutive_bad = _tail_run(verdicts, lambda v: v != "OK")
    trend.consecutive_ok = _tail_run(verdicts, lambda v: v == "OK")

    points: list[int] = []
    for dev in samples:
        value = dev.get("uptime_ticks")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        points.append(int(value))

    if points:
        trend.uptime_ticks = points[-1]
        trend.restarted = any(
            later < earlier for earlier, later in zip(points, points[1:], strict=False)
        )
        if not trend.restarted and len(points) >= 2:
            trend.uptime_delta = points[-1] - points[0]

    counts = [
        int(dev["values"]["ifTable"])
        for dev in samples
        if isinstance(dev.get("values"), dict) and isinstance(dev["values"].get("ifTable"), int)
    ]
    if counts:
        trend.if_count = counts[-1]
        trend.if_count_changes = sum(
            1 for earlier, later in zip(counts, counts[1:], strict=False) if earlier != later
        )

    return trend


def decide(trend: Trend, threshold: int = 3) -> tuple[str, str]:
    """趋势 → (状态, 原因)。纯函数，不读文件、不打印，方便验收直接调。

    规则表：
        样本数 < threshold            → UNKNOWN  数据不足，既不告警也不宣布正常
        最新非 OK 且连续 >= threshold  → ALERT    连续失败才告警（降噪的核心）
        最新非 OK 但连续 < threshold   → OK       单次抖动先观察
        最新 OK                       → OK

    UNKNOWN 不是装饰：只有 1 条历史时说「正常」是假阳性，说「告警」是假阳性
    的镜像 —— 两种都在撒谎，正确回答是「不下结论」。
    """
    if threshold < 1:
        raise ValueError("threshold 必须 >= 1")

    if trend.samples < threshold:
        return STATUS_UNKNOWN, f"历史只有 {trend.samples} 条，不足 {threshold} 条，不下结论"

    if trend.last_verdict != "OK":
        if trend.consecutive_bad >= threshold:
            return STATUS_ALERT, f"连续 {trend.consecutive_bad} 次非 OK"
        return STATUS_OK, f"本次 {trend.last_verdict}，连续 {trend.consecutive_bad} 次，未到阈值"

    if trend.restarted:
        return STATUS_OK, "最近一次 OK，但期间检测到重启（uptime 变小）"
    return STATUS_OK, "最近一次 OK"


def build_trends(
    report_dir: str,
    threshold: int = 3,
) -> tuple[list[Trend], dict]:
    """读目录 → 归组 → 每台设备算一条 Trend（含判定）。"""
    reports = load_reports(report_dir)
    groups = group_by_device(reports)
    trends = []
    for samples in groups.values():
        trend = analyze(samples)
        trend.status, trend.reason = decide(trend, threshold)
        trends.append(trend)
    trends.sort(key=lambda t: (t.host, t.port))
    return trends, groups


def trend_to_dict(trend: Trend) -> dict:
    """Trend → JSON 可写结构（导出用，字段与终端表格一一对应）。"""
    return {
        "host": trend.host,
        "port": trend.port,
        "samples": trend.samples,
        "ok": trend.ok,
        "degraded": trend.degraded,
        "failed": trend.failed,
        "last_verdict": trend.last_verdict,
        "prev_verdict": trend.prev_verdict,
        "consecutive_bad": trend.consecutive_bad,
        "consecutive_ok": trend.consecutive_ok,
        "uptime_ticks": trend.uptime_ticks,
        "uptime_delta": trend.uptime_delta,
        "restarted": trend.restarted,
        "if_count": trend.if_count,
        "if_count_changes": trend.if_count_changes,
        "last_seen": trend.last_seen,
        "status": trend.status,
        "reason": trend.reason,
    }
