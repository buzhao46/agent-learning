"""D17 验收：告警状态机与通知。

B1  首次运行：没有状态文件 + 连续 3 次失败 → 立刻 alert_open，状态落盘，退出码 1
B2  幂等：同一批报告再跑 → 无新事件、退出码 0、日志不增长、since 不变
B3  恢复防抖：ALERT 后 1 次 OK 不消警（recover=2），第 2 次 OK 才 alert_close
B4  UNKNOWN 不改状态：设备从本次报告里消失，状态仍是 ALERT，不是 UNKNOWN
B5  数据过期：last_seen 超过 stale-after → 发一次 stale，再跑不重复
B6  通知失败不回滚：webhook 不可达 → 退出码 2、pending 保留、状态仍 ALERT；
    换成 file 渠道重跑 → pending 清空、日志有记录
B7  dry-run 零副作用：状态文件字节不变、日志不增长，但仍报告“将发什么”
B8  坏状态文件：不崩、备份成 alert_state.bad-*.json、按首次运行处理
B9  静默窗口：抑制通知（不发、不入队），但状态照常推进到 ALERT

说明：本脚本不启动 SNMP，直接构造 scan_*.json 喂给 alert.py。
报告时间戳用「当前时间 - N 分钟」生成，这样 stale 判定是确定性的，不靠 sleep。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "alert.py"
VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"

(ROOT / "out").mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="_d17check_", dir=str(ROOT / "out")))

EXIT_OF = {"OK": 0, "DEGRADED": 2, "FAILED": 1}
FAILED_PORT = 161


def interpreter() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def make_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if "COVERAGE_PROCESS_START" in os.environ:
        env["COVERAGE_PROCESS_START"] = os.environ["COVERAGE_PROCESS_START"]
    return env


def run_alert(*args: str, timeout: float = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [interpreter(), str(ENTRY), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=make_env(),
        timeout=timeout,
    )


def tail(text: str, n: int = 900) -> str:
    body = (text or "").strip()
    return "\n".join(body.splitlines()[-14:])[:n] if body else "(空)"


def device(host: str, port: int, verdict: str) -> dict:
    return {
        "host": host,
        "port": port,
        "verdict": verdict,
        "exit_code": EXIT_OF[verdict],
        "uptime_ticks": 12345678,
        "values": {"ifTable": 3, "sysName": "fake"},
        "failures": []
        if verdict == "OK"
        else [{"stage": "sysDescr", "kind": "timeout", "detail": "假失败", "hint": "检查链路"}],
    }


def summarize(devices: list[dict]) -> dict:
    verdicts = [d["verdict"] for d in devices]
    ok, degraded, failed = (
        verdicts.count("OK"),
        verdicts.count("DEGRADED"),
        verdicts.count("FAILED"),
    )
    worst = "FAILED" if failed else ("DEGRADED" if degraded else "OK")
    return {
        "total": len(devices),
        "ok": ok,
        "degraded": degraded,
        "failed": failed,
        "exit_code": EXIT_OF[worst],
    }


def write_report(
    directory: Path, seq: int, devices: list[dict], *, minutes_ago: float = 0.0
) -> Path:
    """造一份报告。generated_at = 现在 - minutes_ago，让 stale 判定可控。
    文件名必须匹配 scan_*.json，且序号递增 —— load_reports 用 (generated_at, 文件名) 排序。"""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    payload = {
        "schema": 1,
        "generated_at": stamp,
        "summary": summarize(devices),
        "devices": devices,
    }
    path = directory / f"scan_{seq:06d}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def paths(name: str) -> tuple[Path, Path, Path]:
    """每个用例独立的 (报告目录, 状态文件, 日志)。"""
    base = TMP / name
    base.mkdir(parents=True, exist_ok=True)
    return base / "reports", base / "state" / "alert_state.json", base / "alerts.log"


def run(report_dir, state_path, log_path, *extra, channel="file"):
    return run_alert(
        "--report-dir",
        str(report_dir),
        "--state",
        str(state_path),
        "--log",
        str(log_path),
        "--channel",
        channel,
        "--threshold",
        "3",
        "--recover",
        "2",
        *extra,
    )


def read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def seed_alert(name: str, host: str = "10.0.0.40", minutes: tuple[float, ...] = (3.0, 2.0, 1.0)):
    """造出「已经处于 ALERT 且通知已发」的现场：3 份 FAILED + 跑一次 alert。"""
    report_dir, state_path, log_path = paths(name)
    for seq, ago in enumerate(minutes, start=1):
        write_report(report_dir, seq, [device(host, FAILED_PORT, "FAILED")], minutes_ago=ago)
    proc = run(report_dir, state_path, log_path)
    return report_dir, state_path, log_path, proc


def check_first_run() -> tuple[bool, str]:
    report_dir, state_path, log_path, proc = seed_alert("b1")
    out = (proc.stdout or "") + (proc.stderr or "")

    if not state_path.exists():
        return False, f"首次运行没写状态文件\n{tail(out)}"
    state = read_state(state_path)
    label = f"10.0.0.40:{FAILED_PORT}"
    entry = state["devices"].get(label, {})
    log = read_log(log_path)

    problems = []
    if entry.get("status") != "ALERT":
        problems.append(f"状态应为 ALERT，实际 {entry.get('status')!r}")
    if proc.returncode != 1:
        problems.append(f"退出码应为 1（有事件），实际 {proc.returncode}")
    if not log or log[0].get("kind") != "alert_open":
        problems.append(f"日志里没有 alert_open，实际 {[e.get('kind') for e in log]}")
    if state.get("pending"):
        problems.append(f"发送成功却留下了待发事件：{state['pending']}")

    if problems:
        return False, "；".join(problems) + "\n" + tail(out)
    return True, "无状态文件时立即 alert_open，状态落盘，退出码 1"


def check_idempotent() -> tuple[bool, str]:
    report_dir, state_path, log_path, first = seed_alert("b2")
    before = read_state(state_path)
    log_before = read_log(log_path)

    second = run(report_dir, state_path, log_path)
    out = (second.stdout or "") + (second.stderr or "")
    after = read_state(state_path)
    log_after = read_log(log_path)

    problems = []
    if second.returncode != 0:
        problems.append(f"第二次应无新事件、退出码 0，实际 {second.returncode}")
    if len(log_after) != len(log_before):
        problems.append(f"日志从 {len(log_before)} 条涨到 {len(log_after)} 条：重复告警了")
    label = f"10.0.0.40:{FAILED_PORT}"
    if after["devices"][label]["since"] != before["devices"][label]["since"]:
        problems.append("since 被重置（说明状态被当成新告警重开）")
    if after["devices"][label]["status"] != "ALERT":
        problems.append("状态从 ALERT 漂移了")

    if problems:
        return False, "；".join(problems) + "\n" + tail(out)
    return True, "重复运行零事件、日志不增长、since 不变"


def check_recover_debounce() -> tuple[bool, str]:
    report_dir, state_path, log_path, _ = seed_alert("b3")
    label = f"10.0.0.40:{FAILED_PORT}"

    write_report(report_dir, 4, [device("10.0.0.40", FAILED_PORT, "OK")], minutes_ago=0)
    once = run(report_dir, state_path, log_path)
    once_state = read_state(state_path)

    problems = []
    if once_state["devices"][label]["status"] != "ALERT":
        problems.append(
            f"只 1 次 OK（recover=2）就消警了，实际 {once_state['devices'][label]['status']}"
        )
    if any(e.get("kind") == "alert_close" for e in read_log(log_path)):
        problems.append("只 1 次 OK 就发了 alert_close")
    if once.returncode != 0:
        problems.append(f"未到恢复阈值应无事件、退出码 0，实际 {once.returncode}")

    write_report(report_dir, 5, [device("10.0.0.40", FAILED_PORT, "OK")], minutes_ago=0)
    twice = run(report_dir, state_path, log_path)
    twice_state = read_state(state_path)
    closes = [e for e in read_log(log_path) if e.get("kind") == "alert_close"]

    if twice_state["devices"][label]["status"] != "OK":
        problems.append(f"连续 2 次 OK 后应为 OK，实际 {twice_state['devices'][label]['status']}")
    if len(closes) != 1:
        problems.append(f"alert_close 应恰好 1 条，实际 {len(closes)}")
    if twice.returncode != 1:
        problems.append(f"产生 close 事件时退出码应为 1，实际 {twice.returncode}")

    if problems:
        return False, "；".join(problems) + "\n" + tail((twice.stdout or "") + (twice.stderr or ""))
    return True, "1 次 OK 维持 ALERT，连续 2 次 OK 才 alert_close"


def check_unknown_keeps_state() -> tuple[bool, str]:
    report_dir, state_path, log_path, _ = seed_alert("b4")
    label = f"10.0.0.40:{FAILED_PORT}"

    # 换一个只含别的设备的报告目录：X 在状态里、但本次报告完全没有它
    other_dir = TMP / "b4" / "other_reports"
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(other_dir, seq, [device("10.0.0.41", FAILED_PORT, "OK")], minutes_ago=ago)

    proc = run(other_dir, state_path, log_path)
    state = read_state(state_path)
    out = (proc.stdout or "") + (proc.stderr or "")

    problems = []
    if label not in state["devices"]:
        problems.append("消失的设备被从状态里删掉了")
    elif state["devices"][label]["status"] != "ALERT":
        problems.append(
            f"消失后状态变成 {state['devices'][label]['status']}（UNKNOWN 覆盖了 ALERT）"
        )
    if "本次报告里没有" not in out:
        problems.append("没有提示该设备本次无数据")
    if proc.returncode != 0:
        problems.append(f"无新事件时退出码应为 0，实际 {proc.returncode}")

    if problems:
        return False, "；".join(problems) + "\n" + tail(out)
    return True, "设备从报告中消失时保持 ALERT（按无数据处理，不写 UNKNOWN）"


def check_stale() -> tuple[bool, str]:
    report_dir, state_path, log_path, first = seed_alert("b5", minutes=(45.0, 40.0, 35.0))
    out = (first.stdout or "") + (first.stderr or "")
    stales = [e for e in read_log(log_path) if e.get("kind") == "stale"]

    problems = []
    if not stales:
        problems.append(f"数据 35 分钟未更新（阈值 30）却没发 stale\n{tail(out)}")
    if first.returncode != 1:
        problems.append(f"首次应产生事件、退出码 1，实际 {first.returncode}")

    second = run(report_dir, state_path, log_path)
    stales_after = [e for e in read_log(log_path) if e.get("kind") == "stale"]
    if len(stales_after) != len(stales):
        problems.append(f"stale 重复发送：{len(stales)} → {len(stales_after)}")
    if second.returncode != 0:
        problems.append(f"第二次应无新事件、退出码 0，实际 {second.returncode}")

    if problems:
        return False, "；".join(problems)
    return True, f"陈旧数据发 1 条 stale，再跑不重复（共 {len(stales_after)} 条）"


def check_delivery_isolation() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("b6")
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.40", FAILED_PORT, "FAILED")], minutes_ago=ago)

    bad = run(
        report_dir, state_path, log_path, "--webhook", "http://127.0.0.1:1/", channel="webhook"
    )
    state = read_state(state_path)
    label = f"10.0.0.40:{FAILED_PORT}"

    problems = []
    if bad.returncode != 2:
        problems.append(f"投递失败时退出码应为 2，实际 {bad.returncode}")
    if state["devices"][label]["status"] != "ALERT":
        problems.append("投递失败把状态回滚了（会变成重复告警的死循环）")
    if not state.get("pending"):
        problems.append("投递失败却没有进 outbox")

    retry = run(report_dir, state_path, log_path)
    state_after = read_state(state_path)
    if state_after["pending"]:
        problems.append(f"补发后 outbox 没清空：{state_after['pending']}")
    if not read_log(log_path):
        problems.append("补发没有落到 file 渠道的日志里")
    if retry.returncode not in (0, 1):
        problems.append(f"补发成功时退出码不该是 {retry.returncode}")

    if problems:
        return False, "；".join(problems) + "\n" + tail((bad.stdout or "") + (bad.stderr or ""))
    return True, "投递失败退出码 2、pending 保留、状态仍 ALERT；换渠道后补发成功"


def check_dry_run() -> tuple[bool, str]:
    report_dir, state_path, log_path, _ = seed_alert("b7")
    # 造一个「本该消警」的现场：连续 1 次 OK + recover=1
    write_report(report_dir, 4, [device("10.0.0.40", FAILED_PORT, "OK")], minutes_ago=0)

    state_before = state_path.read_bytes()
    log_count_before = len(read_log(log_path))
    proc = run(report_dir, state_path, log_path, "--recover", "1", "--dry-run")
    out = (proc.stdout or "") + (proc.stderr or "")

    problems = []
    if proc.returncode != 1:
        problems.append(f"dry-run 有事件时退出码应为 1，实际 {proc.returncode}")
    if "将发" not in out:
        problems.append("dry-run 没有说明会发什么")
    if state_path.read_bytes() != state_before:
        problems.append("dry-run 改了状态文件")
    if len(read_log(log_path)) != log_count_before:
        problems.append("dry-run 发了通知（日志增长了）")

    if problems:
        return False, "；".join(problems) + "\n" + tail(out)
    return True, "dry-run 只报告不落盘：状态字节不变、日志不增长"


def check_bad_state() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("b8")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{ 这不是 JSON", encoding="utf-8")
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.40", FAILED_PORT, "FAILED")], minutes_ago=ago)

    proc = run(report_dir, state_path, log_path)
    out = (proc.stdout or "") + (proc.stderr or "")

    problems = []
    backups = list(state_path.parent.glob("alert_state.bad-*.json"))
    if not backups:
        problems.append("坏状态文件没有被备份")
    if proc.returncode != 1:
        problems.append(f"应按首次运行产生事件、退出码 1，实际 {proc.returncode}")
    state = read_state(state_path)
    if state["devices"].get(f"10.0.0.40:{FAILED_PORT}", {}).get("status") != "ALERT":
        problems.append("按首次运行后状态不是 ALERT")

    if problems:
        return False, "；".join(problems) + "\n" + tail(out)
    return True, f"坏状态文件已备份为 {backups[0].name if backups else '?'}，按首次运行处理"


def check_silence() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("b9")
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.40", FAILED_PORT, "FAILED")], minutes_ago=ago)

    until = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
    proc = run(report_dir, state_path, log_path, "--silence-until", until)
    state = read_state(state_path)
    out = (proc.stdout or "") + (proc.stderr or "")
    label = f"10.0.0.40:{FAILED_PORT}"

    problems = []
    if "静默" not in out:
        problems.append("没有提示静默")
    if read_log(log_path):
        problems.append("静默期间仍然发了通知")
    if state.get("pending"):
        problems.append("静默期间事件进了 outbox（静默语义是不发，不是延后发）")
    if state["devices"].get(label, {}).get("status") != "ALERT":
        problems.append("静默不该阻止状态推进：状态不是 ALERT")
    if proc.returncode != 1:
        problems.append(f"产生了事件（只是被静默）退出码应为 1，实际 {proc.returncode}")

    if problems:
        return False, "；".join(problems) + "\n" + tail(out)
    return True, "静默只抑制通知：不发、不入队，状态仍推进到 ALERT"


CHECKS = [
    ("B1", "首次运行立即 alert_open", check_first_run),
    ("B2", "重复运行零事件（幂等）", check_idempotent),
    ("B3", "恢复防抖：连续 N 次 OK 才消警", check_recover_debounce),
    ("B4", "设备消失时 UNKNOWN 不覆盖 ALERT", check_unknown_keeps_state),
    ("B5", "数据过期只发一次 stale", check_stale),
    ("B6", "通知失败不回滚状态，outbox 补发", check_delivery_isolation),
    ("B7", "dry-run 零副作用", check_dry_run),
    ("B8", "坏状态文件不致命", check_bad_state),
    ("B9", "静默只抑制通知，不阻止状态推进", check_silence),
]


def main() -> int:
    print("=" * 64)
    print("D17 验收：告警状态机与通知")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")

    missing = [
        name
        for name in ("alert.py", "alerts.py", "state.py", "notify.py")
        if not (ROOT / name).exists()
    ]
    if missing:
        print(f"准备：[!] 缺文件：{', '.join(missing)}")
        return 1
    print("准备：OK")

    passed = 0
    for code, title, func in CHECKS:
        print(f"\n{code}  {title}")
        try:
            ok, detail = func()
        except Exception as exc:  # noqa: BLE001 - 验收脚本：异常记成失败，别中断整轮
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"    [{'OK' if ok else 'NG'}] {detail}")
        passed += 1 if ok else 0

    total = len(CHECKS)
    print("\n" + "-" * 64)
    print(f"通过 {passed}/{total}")
    print("结论：全绿。告警只在状态变化时发一次。" if passed == total else "结论：还没全绿。")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
