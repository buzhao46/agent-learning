"""D18 验收：告警收敛与升级（ack / escalation / correlation）。

C1  ack 后即使超时也不再升级
C2  未 ack 超时升级一次，重复跑不刷屏（level 只 +1）
C3  同组多台同时掉线只发 1 条 group_alert，单设备 open 被抑制
C4  组恢复到阈值以下 → 发恢复 group_alert，组状态清回 OK
C5  升级到 max_level 后停止（共 N 次）
C6  ack 单台与按组批量均生效，状态持久
C7  组恢复不污染单设备状态（组内仍坏的设备保持 ALERT）
C8  回归：D17 仍 9/9

说明：本脚本不启动 SNMP，直接构造 scan_*.json。报告时间戳用「现在 - N 分钟」，
这样 stale / escalate 判定是确定性的，不靠 sleep。
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
CHECK_D17 = ROOT / "check_d17.py"
VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"

(ROOT / "out").mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="_d18check_", dir=str(ROOT / "out")))

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


def run_alert(report_dir, state_path, log_path, *extra, timeout: float = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [interpreter(), str(ALERT), "--report-dir", str(report_dir),
         "--state", str(state_path), "--log", str(log_path), "--channel", "file", *extra],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=make_env(), timeout=timeout,
    )


def run_ack(state_path, *extra, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [interpreter(), str(ACK), "--state", str(state_path), *extra],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=make_env(), timeout=timeout,
    )


def device(host: str, port: int, verdict: str) -> dict:
    return {
        "host": host, "port": port, "verdict": verdict,
        "exit_code": EXIT_OF[verdict],
        "uptime_ticks": 12345678,
        "values": {"ifTable": 3, "sysName": "fake"},
        "failures": []
        if verdict == "OK" else [{"stage": "sysDescr", "kind": "timeout", "detail": "假失败", "hint": "检查链路"}],
    }


def summarize(devices: list[dict]) -> dict:
    verdicts = [d["verdict"] for d in devices]
    worst = "FAILED" if "FAILED" in verdicts else ("DEGRADED" if "DEGRADED" in verdicts else "OK")
    return {"total": len(devices), "ok": verdicts.count("OK"),
            "degraded": verdicts.count("DEGRADED"), "failed": verdicts.count("FAILED"),
            "exit_code": EXIT_OF[worst]}


def write_report(directory: Path, seq: int, devices: list[dict], *, minutes_ago: float = 0.0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    payload = {"schema": 1, "generated_at": stamp, "summary": summarize(devices), "devices": devices}
    path = directory / f"scan_{seq:06d}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_groups(base: Path, mapping: dict[str, list[str]]) -> Path:
    path = base / "groups.json"
    path.write_text(json.dumps({"groups": [{"name": n, "members": m} for n, m in mapping.items()]},
                               ensure_ascii=False), encoding="utf-8")
    return path


def paths(name: str) -> tuple[Path, Path, Path]:
    base = TMP / name
    base.mkdir(parents=True, exist_ok=True)
    return base / "reports", base / "state" / "alert_state.json", base / "alerts.log"


def read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def rewind_since(state_path: Path, label: str, minutes_ago: float) -> None:
    """把某设备的 since 拨回过去，模拟「这条告警已经开了很久」。

    step 在 open 时会把 since 设为「现在」，所以单跑一次 alert 的 since 永远是现在。
    要测升级/超时，必须人为把 since 拨到 N 分钟前——等价于「这台设备 60 分钟前
    就进 ALERT 了，但我们刚跑第一次巡检」。
    """
    state = read_state(state_path)
    since = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    dev = state["devices"].setdefault(label, {})
    dev["status"] = "ALERT"
    dev["since"] = since
    dev["escalation_level"] = 0
    dev["last_escalated_at"] = None
    dev["acknowledged"] = False
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def seed(report_dir, state_path, log_path, devices, agos=(3.0, 2.0, 1.0), extra=()):
    for seq, ago in enumerate(agos, start=1):
        write_report(report_dir, seq, devices, minutes_ago=ago)
    return run_alert(report_dir, state_path, log_path, *extra)


# ---------------------------------------------------------------- C1
def check_ack_blocks_escalation() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c1")
    for seq, ago in enumerate((60.0, 59.0, 58.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.40", 161, "FAILED")], minutes_ago=ago)

    run_alert(report_dir, state_path, log_path)            # 先 open，since=now
    rewind_since(state_path, "10.0.0.40:161", 60)         # 模拟已开 60 分钟

    p1 = run_alert(report_dir, state_path, log_path, "--escalate-channel", "file")
    esc1 = [e for e in read_log(log_path) if e.get("kind") == "alert_escalate"]
    problems = []
    if len(esc1) != 1:
        problems.append(f"未确认时应升级 1 次，实际 {len(esc1)}")

    pa = run_ack(state_path, "--device", "10.0.0.40:161", "--by", "alice")
    if pa.returncode != 0:
        problems.append(f"ack 失败 rc={pa.returncode}")
    state = read_state(state_path)
    if not state["devices"]["10.0.0.40:161"].get("acknowledged"):
        problems.append("ack 后 acknowledged 未置 True")

    p2 = run_alert(report_dir, state_path, log_path)
    esc2 = [e for e in read_log(log_path) if e.get("kind") == "alert_escalate"]
    if len(esc2) != len(esc1):
        problems.append(f"ack 后不应再升级，escalate {len(esc1)}→{len(esc2)}")
    if problems:
        return False, "；".join(problems) + "\n" + tail((p2.stdout or "") + (p2.stderr or ""))
    return True, "ack 后即使超时也不再升级，acknowledged 持久"


# ---------------------------------------------------------------- C2
def check_escalate_once() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c2")
    for seq, ago in enumerate((40.0, 39.0, 38.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.40", 161, "FAILED")], minutes_ago=ago)

    run_alert(report_dir, state_path, log_path)
    rewind_since(state_path, "10.0.0.40:161", 40)

    p1 = run_alert(report_dir, state_path, log_path, "--escalate-channel", "file")
    esc1 = [e for e in read_log(log_path) if e.get("kind") == "alert_escalate"]
    state1 = read_state(state_path)
    problems = []
    if len(esc1) != 1:
        problems.append(f"首次应升级 1 次，实际 {len(esc1)}")
    if state1["devices"]["10.0.0.40:161"].get("escalation_level") != 1:
        problems.append("首次应到 L1")

    p2 = run_alert(report_dir, state_path, log_path)
    esc2 = [e for e in read_log(log_path) if e.get("kind") == "alert_escalate"]
    if len(esc2) != 1:
        problems.append(f"重复跑应稳定为 1 次，实际 {len(esc2)}")
    if problems:
        return False, "；".join(problems) + "\n" + tail((p2.stdout or "") + (p2.stderr or ""))
    return True, "未 ack 超时升级一次，重复跑不刷屏"


# ---------------------------------------------------------------- C3
def check_correlation() -> tuple[bool, str]:
    name = "c3"
    report_dir, state_path, log_path = paths(name)
    devs = [device("10.0.0.9", 161, "FAILED"), device("10.0.0.9", 162, "FAILED"), device("10.0.0.9", 163, "FAILED")]
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, devs, minutes_ago=ago)
    gpath = write_groups(TMP / name, {"机房A": ["10.0.0.9:161", "10.0.0.9:162", "10.0.0.9:163"]})

    p = run_alert(report_dir, state_path, log_path, "--groups", str(gpath), "--escalate-channel", "file")
    log = read_log(log_path)
    opens = [e for e in log if e.get("kind") == "alert_open"]
    groups = [e for e in log if e.get("kind") == "group_alert"]
    state = read_state(state_path)
    problems = []
    if len(groups) != 1:
        problems.append(f"group_alert 应 1 条，实际 {len(groups)}")
    if len(opens) != 0:
        problems.append(f"单设备 open 应被收敛为 0，实际 {len(opens)}")
    if state["groups"].get("机房A", {}).get("status") != "ALERT":
        problems.append("组状态未置 ALERT")
    for lbl in ("10.0.0.9:161", "10.0.0.9:162", "10.0.0.9:163"):
        if state["devices"].get(lbl, {}).get("status") != "ALERT":
            problems.append(f"{lbl} 应仍 ALERT")
    if problems:
        return False, "；".join(problems) + "\n" + tail((p.stdout or "") + (p.stderr or ""))
    return True, "同组 3 台同时掉线只发 1 条 group_alert，单设备 open 被抑制"


# ---------------------------------------------------------------- C4 + C7
def check_group_recovery() -> tuple[bool, str]:
    name = "c4"
    report_dir, state_path, log_path = paths(name)
    devs = [device("10.0.0.9", 161, "FAILED"), device("10.0.0.9", 162, "FAILED"), device("10.0.0.9", 163, "FAILED")]
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, devs, minutes_ago=ago)
    gpath = write_groups(TMP / name, {"机房A": ["10.0.0.9:161", "10.0.0.9:162", "10.0.0.9:163"]})

    run_alert(report_dir, state_path, log_path, "--groups", str(gpath), "--escalate-channel", "file")  # c3 现场
    # 追加两份 A,B 恢复、C 仍坏的报告 → A,B 连续 2 次 OK 消警，C 仍 ALERT，组低于阈值
    for seq in (4, 5):
        write_report(report_dir, seq,
                     [device("10.0.0.9", 161, "OK"), device("10.0.0.9", 162, "OK"), device("10.0.0.9", 163, "FAILED")],
                     minutes_ago=0)
    p = run_alert(report_dir, state_path, log_path, "--groups", str(gpath), "--escalate-channel", "file")

    log = read_log(log_path)
    rec = [e for e in log if e.get("kind") == "group_alert" and e.get("new_status") == "OK"]
    state = read_state(state_path)
    problems = []
    if len(rec) != 1:
        problems.append(f"恢复 group_alert 应 1 条，实际 {len(rec)}")
    if state["groups"]["机房A"]["status"] != "OK":
        problems.append("组状态应恢复 OK")
    # C7：组内仍坏的设备不能被组恢复误标 OK
    if state["devices"]["10.0.0.9:163"].get("status") != "ALERT":
        problems.append("组恢复把组内仍坏的设备误标 OK")
    if problems:
        return False, "；".join(problems) + "\n" + tail((p.stdout or "") + (p.stderr or ""))
    return True, "组恢复发明细且清回 OK，组内仍坏设备保持 ALERT（C4+C7）"


# ---------------------------------------------------------------- C5
def check_escalate_cap() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c5")
    for seq, ago in enumerate((200.0, 199.0, 198.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.40", 161, "FAILED")], minutes_ago=ago)
    run_alert(report_dir, state_path, log_path)
    rewind_since(state_path, "10.0.0.40:161", 200)
    for _ in range(3):
        run_alert(report_dir, state_path, log_path, "--max-level", "2", "--escalate-after", "30", "--escalate-channel", "file")
    log = read_log(log_path)
    esc = [e for e in log if e.get("kind") == "alert_escalate"]
    state = read_state(state_path)
    lvl = state["devices"]["10.0.0.40:161"].get("escalation_level", 0)
    problems = []
    if lvl != 2:
        problems.append(f"应停在 L2，实际 L{lvl}")
    if len(esc) != 2:
        problems.append(f"应升级 2 次，实际 {len(esc)}")
    if problems:
        return False, "；".join(problems)
    return True, "升级到 max_level=2 后停止，共 2 次"


# ---------------------------------------------------------------- C6
def check_ack_cli() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c6")
    devs = [device("10.0.0.9", 161, "FAILED"), device("10.0.0.9", 162, "FAILED")]
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, devs, minutes_ago=ago)
    gpath = write_groups(TMP / "c6", {"机房A": ["10.0.0.9:161", "10.0.0.9:162"]})
    run_alert(report_dir, state_path, log_path, "--groups", str(gpath))

    pa = run_ack(state_path, "--device", "10.0.0.9:161", "--by", "alice")
    s1 = read_state(state_path)
    problems = []
    if pa.returncode != 0:
        problems.append("ack 单台失败")
    if not s1["devices"]["10.0.0.9:161"].get("acknowledged"):
        problems.append("单台 ack 未生效")

    pg = run_ack(state_path, "--group", "机房A", "--by", "bob", "--groups", str(gpath))
    s2 = read_state(state_path)
    if pg.returncode != 0:
        problems.append("ack 组失败")
    if not s2["devices"]["10.0.0.9:162"].get("acknowledged"):
        problems.append("组内另一台未被批量 ack")
    if problems:
        return False, "；".join(problems) + "\n" + (pa.stdout or "") + (pg.stdout or "")
    return True, "ack 单台与按组批量均生效，状态持久"


# ---------------------------------------------------------------- C8
def check_d17_regression() -> tuple[bool, str]:
    proc = subprocess.run(
        [interpreter(), str(CHECK_D17)], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=make_env(), timeout=180,
    )
    if proc.returncode != 0:
        return False, f"D17 回归挂了 rc={proc.returncode}\n" + tail(proc.stdout + proc.stderr)
    return True, "D17 仍 9/9"


CHECKS = [
    ("C1", "ack 阻止升级", check_ack_blocks_escalation),
    ("C2", "未 ack 超时升级一次且稳定", check_escalate_once),
    ("C3", "同组批量故障收敛为 1 条", check_correlation),
    ("C4", "组恢复发明细并清回 OK", check_group_recovery),
    ("C5", "升级上限 max_level", check_escalate_cap),
    ("C6", "ack 单台与按组批量", check_ack_cli),
    ("C7", "组状态与单设备状态解耦", check_group_recovery),
    ("C8", "D17 回归 9/9", check_d17_regression),
]


def main() -> int:
    print("=" * 64)
    print("D18 验收：告警收敛与升级")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")
    missing = [n for n in ("alert.py", "alerts.py", "state.py", "notify.py", "ack.py") if not (ROOT / n).exists()]
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
    print("结论：全绿。告警收敛与升级跑通了。" if passed == total else "结论：还没全绿。")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
