"""D20 验收：通知聚合（digest）—— 把同一轮巡检里的新告警 / 升级聚成一条摘要。

C1  单台告警仍单独发 alert_open，不聚合
C2  3 台未分组同时挂 → 只发 1 条 alert_digest（不再 3 条 alert_open）
C3  digest 每条 items 都带各自的确认链接（device 链接）
C4  3 台同时升级 → 只发 1 条 escalate_digest（含级别/等待时长上下文），无单独 alert_escalate
C5  组收敛与 digest 共存：组内 3 台 → 1 条 group_alert；组外 2 台散装 → 1 条 alert_digest(2)，无单独 open
C6  阈值控制：--digest-threshold 3，2 台挂仍单独发（不聚合）
C7  --no-digest 关闭聚合：3 台挂发 3 条 alert_open
C8  回归：D19（8/8）+ D18（8/8）+ D17（9/9）

场景构造手法和 D17–D19 一致：直接写 scan_*.json，时间戳用「现在 - N 分钟」；
升级场景用 rewind_since 把 since 拨回过去模拟「已开很久」。
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
ALERT = ROOT / "alert.py"
ACK = ROOT / "ack.py"
CHECK_D19 = ROOT / "check_d19.py"
CHECK_D18 = ROOT / "check_d18.py"
CHECK_D17 = ROOT / "check_d17.py"
VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"

(ROOT / "out").mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="_d20check_", dir=str(ROOT / "out")))

EXIT_OF = {"OK": 0, "DEGRADED": 2, "FAILED": 1}


def interpreter() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def make_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


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
    worst = "FAILED" if "FAILED" in verdicts else ("DEGRADED" if "DEGRADED" in verdicts else "OK")
    return {
        "total": len(devices),
        "ok": verdicts.count("OK"),
        "degraded": verdicts.count("DEGRADED"),
        "failed": verdicts.count("FAILED"),
        "exit_code": EXIT_OF[worst],
    }


def write_report(
    directory: Path, seq: int, devices: list[dict], *, minutes_ago: float = 0.0
) -> Path:
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


def write_groups(base: Path, mapping: dict[str, list[str]]) -> Path:
    path = base / "groups.json"
    path.write_text(
        json.dumps(
            {"groups": [{"name": n, "members": m} for n, m in mapping.items()]}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    return path


def paths(name: str) -> tuple[Path, Path, Path]:
    base = TMP / name
    base.mkdir(parents=True, exist_ok=True)
    return base / "reports", base / "state" / "alert_state.json", base / "alerts.log"


def read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def rewind_since(state_path: Path, label: str, minutes_ago: float) -> None:
    state = read_state(state_path)
    since = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    dev = state["devices"].setdefault(label, {})
    dev["status"] = "ALERT"
    dev["since"] = since
    dev["escalation_level"] = 0
    dev["last_escalated_at"] = None
    dev["acknowledged"] = False
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def run_alert(
    report_dir, state_path, log_path, *extra, timeout: float = 90
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            interpreter(),
            str(ALERT),
            "--report-dir",
            str(report_dir),
            "--state",
            str(state_path),
            "--log",
            str(log_path),
            "--channel",
            "file",
            *extra,
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=make_env(),
        timeout=timeout,
    )


# ---------------------------------------------------------------- C1
def check_single_still_individual() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c1")
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.40", 161, "FAILED")], minutes_ago=ago)
    run_alert(report_dir, state_path, log_path)
    log = read_log(log_path)
    opens = [e for e in log if e.get("kind") == "alert_open"]
    digests = [e for e in log if e.get("kind") == "alert_digest"]
    problems = []
    if len(opens) != 1:
        problems.append(f"单台应 1 条 alert_open，实际 {len(opens)}")
    if digests:
        problems.append("单台不应聚合")
    if problems:
        return False, "；".join(problems)
    return True, "单台告警仍单独发 alert_open，不聚合"


# ---------------------------------------------------------------- C2
def check_three_ungrouped_digest() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c2")
    devs = [
        device("10.0.0.9", 161, "FAILED"),
        device("10.0.0.9", 162, "FAILED"),
        device("10.0.0.9", 163, "FAILED"),
    ]
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, devs, minutes_ago=ago)
    base = "http://127.0.0.1:8731"
    run_alert(report_dir, state_path, log_path, "--ack-base-url", base)
    log = read_log(log_path)
    opens = [e for e in log if e.get("kind") == "alert_open"]
    digests = [e for e in log if e.get("kind") == "alert_digest"]
    problems = []
    if opens:
        problems.append(f"聚合后不应有单独 alert_open，实际 {len(opens)}")
    if len(digests) != 1:
        problems.append(f"应 1 条 alert_digest，实际 {len(digests)}")
    elif len(digests[0].get("items", [])) != 3:
        problems.append(f"digest 应含 3 个 items，实际 {len(digests[0].get('items', []))}")
    if problems:
        return False, "；".join(problems)
    return True, "3 台未分组同时挂 → 1 条 alert_digest（不再 3 条 open）"


# ---------------------------------------------------------------- C3
def check_digest_items_have_links() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c3")
    devs = [
        device("10.0.0.9", 161, "FAILED"),
        device("10.0.0.9", 162, "FAILED"),
        device("10.0.0.9", 163, "FAILED"),
    ]
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, devs, minutes_ago=ago)
    base = "http://127.0.0.1:8731"
    run_alert(report_dir, state_path, log_path, "--ack-base-url", base)
    log = read_log(log_path)
    digest = next((e for e in log if e.get("kind") == "alert_digest"), None)
    problems = []
    if not digest:
        problems.append("没有 alert_digest")
    else:
        for it in digest.get("items", []):
            u = it.get("ack_url") or ""
            if "device=" not in u or "/ack?" not in u:
                problems.append(f"items 确认链接格式不对：{u}")
    if problems:
        return False, "；".join(problems)
    return True, "digest 每条 items 都带各自的确认链接（device 链接）"


# ---------------------------------------------------------------- C4
def check_escalation_digest() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c4")
    devs = [
        device("10.0.0.9", 161, "FAILED"),
        device("10.0.0.9", 162, "FAILED"),
        device("10.0.0.9", 163, "FAILED"),
    ]
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, devs, minutes_ago=ago)
    run_alert(report_dir, state_path, log_path)  # 先 open，since=now
    for lbl in ("10.0.0.9:161", "10.0.0.9:162", "10.0.0.9:163"):
        rewind_since(state_path, lbl, 200)
    run_alert(report_dir, state_path, log_path, "--escalate-channel", "file")  # 3 台同时升级
    log = read_log(log_path)
    esc = [e for e in log if e.get("kind") == "alert_escalate"]
    edig = [e for e in log if e.get("kind") == "escalate_digest"]
    problems = []
    if esc:
        problems.append(f"聚合后不应有单独 alert_escalate，实际 {len(esc)}")
    if len(edig) != 1:
        problems.append(f"应 1 条 escalate_digest，实际 {len(edig)}")
    elif len(edig[0].get("items", [])) != 3:
        problems.append(f"升级摘要应含 3 个 items，实际 {len(edig[0].get('items', []))}")
    else:
        it = edig[0]["items"][0]
        if it.get("level") != 1:
            problems.append(f"升级摘要 item 级别应为 L1，实际 {it.get('level')}")
        if not isinstance(it.get("waited_minutes"), int):
            problems.append("升级摘要 item 缺等待时长上下文")
    if problems:
        return False, "；".join(problems)
    return True, "3 台同时升级 → 1 条 escalate_digest（含级别+等待时长上下文）"


# ---------------------------------------------------------------- C5
def check_group_and_digest_coexist() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c5")
    # 机房A 3 台 + 2 台散装（不在任何组）
    grouped = [
        device("10.0.0.9", 161, "FAILED"),
        device("10.0.0.9", 162, "FAILED"),
        device("10.0.0.9", 163, "FAILED"),
    ]
    loose = [device("10.0.0.40", 161, "FAILED"), device("10.0.0.40", 162, "FAILED")]
    devs = grouped + loose
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, devs, minutes_ago=ago)
    gpath = write_groups(TMP / "c5", {"机房A": ["10.0.0.9:161", "10.0.0.9:162", "10.0.0.9:163"]})
    base = "http://127.0.0.1:8731"
    run_alert(
        report_dir,
        state_path,
        log_path,
        "--groups",
        str(gpath),
        "--ack-base-url",
        base,
        "--escalate-channel",
        "file",
    )
    log = read_log(log_path)
    group_alerts = [e for e in log if e.get("kind") == "group_alert"]
    digests = [e for e in log if e.get("kind") == "alert_digest"]
    opens = [e for e in log if e.get("kind") == "alert_open"]
    problems = []
    if len(group_alerts) != 1:
        problems.append(f"组内 3 台应 1 条 group_alert，实际 {len(group_alerts)}")
    if opens:
        problems.append(f"组内 open 应被收敛掉，实际还有 {len(opens)} 条")
    if len(digests) != 1:
        problems.append(f"组外 2 台应 1 条 alert_digest，实际 {len(digests)}")
    elif len(digests[0].get("items", [])) != 2:
        problems.append(f"散装 digest 应含 2 个 items，实际 {len(digests[0].get('items', []))}")
    if problems:
        return False, "；".join(problems)
    return True, "组收敛(group_alert)与 digest(散装)共存，互不干扰"


# ---------------------------------------------------------------- C6
def check_threshold_control() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c6")
    devs = [device("10.0.0.9", 161, "FAILED"), device("10.0.0.9", 162, "FAILED")]
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, devs, minutes_ago=ago)
    run_alert(report_dir, state_path, log_path, "--digest-threshold", "3")
    log = read_log(log_path)
    opens = [e for e in log if e.get("kind") == "alert_open"]
    digests = [e for e in log if e.get("kind") == "alert_digest"]
    problems = []
    if len(opens) != 2:
        problems.append(f"阈值 3 时 2 台应单独发 2 条 open，实际 {len(opens)}")
    if digests:
        problems.append("阈值 3 时 2 台不应聚合")
    if problems:
        return False, "；".join(problems)
    return True, "阈值控制：--digest-threshold 3 时 2 台仍单独发"


# ---------------------------------------------------------------- C7
def check_no_digest_flag() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c7")
    devs = [
        device("10.0.0.9", 161, "FAILED"),
        device("10.0.0.9", 162, "FAILED"),
        device("10.0.0.9", 163, "FAILED"),
    ]
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, devs, minutes_ago=ago)
    run_alert(report_dir, state_path, log_path, "--no-digest")
    log = read_log(log_path)
    opens = [e for e in log if e.get("kind") == "alert_open"]
    digests = [e for e in log if e.get("kind") == "alert_digest"]
    problems = []
    if len(opens) != 3:
        problems.append(f"--no-digest 应 3 条单独 open，实际 {len(opens)}")
    if digests:
        problems.append("--no-digest 不应出现 digest")
    if problems:
        return False, "；".join(problems)
    return True, "--no-digest 关闭聚合：3 台挂发 3 条 alert_open"


# ---------------------------------------------------------------- C8
def check_regression() -> tuple[bool, str]:
    problems = []
    for label, script in (("D19", CHECK_D19), ("D18", CHECK_D18), ("D17", CHECK_D17)):
        proc = subprocess.run(
            [interpreter(), str(script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=make_env(),
            timeout=300,
        )
        if proc.returncode != 0:
            problems.append(
                f"{label} 回归挂了 rc={proc.returncode}\n" + tail(proc.stdout + proc.stderr)
            )
    if problems:
        return False, "；".join(problems)
    return True, "D19 仍 8/8，D18 仍 8/8，D17 仍 9/9"


CHECKS = [
    ("C1", "单台仍单独发", check_single_still_individual),
    ("C2", "3 台未分组聚合", check_three_ungrouped_digest),
    ("C3", "digest items 带链接", check_digest_items_have_links),
    ("C4", "升级聚合", check_escalation_digest),
    ("C5", "组与 digest 共存", check_group_and_digest_coexist),
    ("C6", "阈值控制", check_threshold_control),
    ("C7", "可关闭聚合", check_no_digest_flag),
    ("C8", "D19/D18/D17 回归", check_regression),
]


def main() -> int:
    print("=" * 64)
    print("D20 验收：通知聚合（digest）")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")
    missing = [
        n
        for n in (
            "alert.py",
            "alerts.py",
            "notify.py",
            "check_d19.py",
            "check_d18.py",
            "check_d17.py",
        )
        if not (ROOT / n).exists()
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
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"    [{'OK' if ok else 'NG'}] {detail}")
        passed += 1 if ok else 0

    total = len(CHECKS)
    print("\n" + "-" * 64)
    print(f"通过 {passed}/{total}")
    print("结论：全绿。通知聚合跑通了。" if passed == total else "结论：还没全绿。")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
