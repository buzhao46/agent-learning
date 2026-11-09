"""D21 验收：值班排班 —— ack 链接 by= 默认填当前值班人，升级摘要 @ 值班人。

C1  无 roster 时 ack 链接不带 by（向后兼容 D19）
C2  配 roster 且日期/班次命中 → ack 链接带 by=张三
C3  组确认链接也带 by（group_ack_url）
C4  roster 日期不匹配（今天没排班）→ by 为 None，链接不带 by
C5  升级摘要 reason 含 @值班人（escalate_digest）
C6  端到端：带 by=张三 的链接点一下 → 状态 acknowledged=True 且 acked_by=张三
C7  by 是自由字段：把链接里 by=张三 改成 by=李四 再点 → acked_by=李四
C8  回归：D20（8/8）+ D19（8/8）+ D18（8/8）+ D17（9/9）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALERT = ROOT / "alert.py"
ACK_SERVER = ROOT / "ack_server.py"
CHECK_D20 = ROOT / "check_d20.py"
CHECK_D19 = ROOT / "check_d19.py"
CHECK_D18 = ROOT / "check_d18.py"
CHECK_D17 = ROOT / "check_d17.py"
VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"
SECRET = "d21-test-secret"

(ROOT / "out").mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="_d21check_", dir=str(ROOT / "out")))

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
        "host": host, "port": port, "verdict": verdict,
        "exit_code": EXIT_OF[verdict], "uptime_ticks": 12345678,
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


def write_roster(base: Path, rows: list[dict]) -> Path:
    path = base / "roster.json"
    path.write_text(json.dumps({"roster": rows}, ensure_ascii=False), encoding="utf-8")
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
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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


def run_alert(report_dir, state_path, log_path, *extra, timeout: float = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [interpreter(), str(ALERT), "--report-dir", str(report_dir),
         "--state", str(state_path), "--log", str(log_path), "--channel", "file", *extra],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=make_env(), timeout=timeout,
    )


def start_server(state_path, groups_path, secret, port, timeout: float = 10) -> subprocess.Popen[str]:
    cmd = [interpreter(), str(ACK_SERVER), "--state", str(state_path),
           "--port", str(port), "--secret", secret, "--host", "127.0.0.1"]
    if groups_path:
        cmd += ["--groups", str(groups_path)]
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=make_env(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board", timeout=1)
            return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("ack_server 启动即退出")
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("ack_server 没起来")


def get_url(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def with_by(url: str, new_by: str) -> str:
    parsed = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(parsed.query)
    q["by"] = [new_by]
    return parsed._replace(query=urllib.parse.urlencode(q, doseq=True)).geturl()


TODAY = datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- C1
def check_no_roster_no_by() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c1")
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.40", 161, "FAILED")], minutes_ago=ago)
    base = "http://127.0.0.1:8731"
    run_alert(report_dir, state_path, log_path, "--ack-base-url", base)
    log = read_log(log_path)
    opens = [e for e in log if e.get("kind") == "alert_open" and e.get("ack_url")]
    problems = []
    if not opens:
        problems.append("没有带 ack_url 的 alert_open")
    elif "by=" in opens[0]["ack_url"]:
        problems.append("无 roster 时链接不应带 by=")
    if problems:
        return False, "；".join(problems)
    return True, "无 roster 时 ack 链接不带 by（向后兼容 D19）"


# ---------------------------------------------------------------- C2
def check_roster_hit_by() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c2")
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.9", 161, "FAILED")], minutes_ago=ago)
    rpath = write_roster(TMP / "c2", [{"date": TODAY, "shift": "day", "person": "张三"}])
    base = "http://127.0.0.1:8731"
    run_alert(report_dir, state_path, log_path, "--ack-base-url", base,
              "--roster", str(rpath), "--shift", "day")
    log = read_log(log_path)
    opens = [e for e in log if e.get("kind") == "alert_open" and e.get("ack_url")]
    problems = []
    if not opens:
        problems.append("没有带 ack_url 的 alert_open")
    else:
        u = urllib.parse.unquote(opens[0]["ack_url"])
        if "by=" not in opens[0]["ack_url"] or "张三" not in u:
            problems.append(f"命中排班时链接应带 by=张三，实际 {opens[0]['ack_url']}")
    if problems:
        return False, "；".join(problems)
    return True, "配 roster 且命中 → ack 链接带 by=张三"


# ---------------------------------------------------------------- C3
def check_group_link_has_by() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c3")
    devs = [device("10.0.0.9", 161, "FAILED"), device("10.0.0.9", 162, "FAILED"),
            device("10.0.0.9", 163, "FAILED")]
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, devs, minutes_ago=ago)
    gpath = write_groups(TMP / "c3", {"机房A": ["10.0.0.9:161", "10.0.0.9:162", "10.0.0.9:163"]})
    rpath = write_roster(TMP / "c3", [{"date": TODAY, "shift": "day", "person": "张三"}])
    base = "http://127.0.0.1:8731"
    run_alert(report_dir, state_path, log_path, "--groups", str(gpath),
              "--ack-base-url", base, "--roster", str(rpath), "--shift", "day",
              "--escalate-channel", "file")
    log = read_log(log_path)
    groups = [e for e in log if e.get("kind") == "group_alert" and e.get("ack_url")]
    problems = []
    if not groups:
        problems.append("没有带 ack_url 的 group_alert")
    else:
        u = urllib.parse.unquote(groups[0]["ack_url"])
        if "by=" not in groups[0]["ack_url"] or "张三" not in u:
            problems.append(f"组链接应带 by=张三，实际 {groups[0]['ack_url']}")
    if problems:
        return False, "；".join(problems)
    return True, "组确认链接也带 by（group_ack_url）"


# ---------------------------------------------------------------- C4
def check_roster_mismatch_no_by() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c4")
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.9", 161, "FAILED")], minutes_ago=ago)
    # 排的是 2000-01-01，今天不命中
    rpath = write_roster(TMP / "c4", [{"date": "2000-01-01", "shift": "day", "person": "张三"}])
    base = "http://127.0.0.1:8731"
    run_alert(report_dir, state_path, log_path, "--ack-base-url", base,
              "--roster", str(rpath), "--shift", "day")
    log = read_log(log_path)
    opens = [e for e in log if e.get("kind") == "alert_open" and e.get("ack_url")]
    problems = []
    if not opens:
        problems.append("没有带 ack_url 的 alert_open")
    elif "by=" in opens[0]["ack_url"]:
        problems.append("今天没排班时链接不应带 by=")
    if problems:
        return False, "；".join(problems)
    return True, "roster 日期不匹配 → 链接不带 by"


# ---------------------------------------------------------------- C5
def check_escalate_digest_mentions_on_duty() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c5")
    devs = [device("10.0.0.9", 161, "FAILED"), device("10.0.0.9", 162, "FAILED"),
            device("10.0.0.9", 163, "FAILED")]
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, devs, minutes_ago=ago)
    run_alert(report_dir, state_path, log_path)  # 先 open
    for lbl in ("10.0.0.9:161", "10.0.0.9:162", "10.0.0.9:163"):
        rewind_since(state_path, lbl, 200)
    rpath = write_roster(TMP / "c5", [{"date": TODAY, "shift": "day", "person": "张三"}])
    base = "http://127.0.0.1:8731"
    run_alert(report_dir, state_path, log_path, "--roster", str(rpath), "--shift", "day",
              "--escalate-channel", "file", "--ack-base-url", base)
    log = read_log(log_path)
    edig = [e for e in log if e.get("kind") == "escalate_digest"]
    problems = []
    if not edig:
        problems.append("没有 escalate_digest")
    elif "张三" not in edig[0].get("reason", ""):
        problems.append(f"升级摘要应 @值班人 张三，实际 reason={edig[0].get('reason')}")
    if problems:
        return False, "；".join(problems)
    return True, "升级摘要 reason 含 @值班人"


# ---------------------------------------------------------------- C6
def check_end_to_end_ack_by() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c6")
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.9", 161, "FAILED")], minutes_ago=ago)
    rpath = write_roster(TMP / "c6", [{"date": TODAY, "shift": "day", "person": "张三"}])
    port = 8751
    base = f"http://127.0.0.1:{port}"
    run_alert(report_dir, state_path, log_path, "--ack-base-url", base,
              "--roster", str(rpath), "--shift", "day", "--ack-secret", SECRET)
    log = read_log(log_path)
    opens = [e for e in log if e.get("kind") == "alert_open" and e.get("ack_url")]
    problems = []
    if not opens:
        problems.append("没有带 ack_url 的 alert_open")
    if problems:
        return False, "；".join(problems)
    url = opens[0]["ack_url"]
    if "张三" not in urllib.parse.unquote(url):
        return False, f"链接未带 by=张三：{url}"
    proc = None
    try:
        proc = start_server(state_path, None, SECRET, port)
        status, _ = get_url(url)
        if status != 200:
            return False, f"点链接返回 {status}，应 200"
        st = read_state(state_path)
        dev = st["devices"]["10.0.0.9:161"]
        if not dev.get("acknowledged"):
            return False, "点链接后未置 acknowledged"
        if dev.get("acked_by") != "张三":
            return False, f"acked_by 应为 张三，实际 {dev.get('acked_by')}"
    finally:
        if proc is not None:
            proc.terminate()
    return True, "带 by=张三 的链接点一下 → acknowledged=True 且 acked_by=张三"


# ---------------------------------------------------------------- C7
def check_mutable_by() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c7")
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.9", 162, "FAILED")], minutes_ago=ago)
    rpath = write_roster(TMP / "c7", [{"date": TODAY, "shift": "day", "person": "张三"}])
    port = 8752
    base = f"http://127.0.0.1:{port}"
    run_alert(report_dir, state_path, log_path, "--ack-base-url", base,
              "--roster", str(rpath), "--shift", "day", "--ack-secret", SECRET)
    log = read_log(log_path)
    opens = [e for e in log if e.get("kind") == "alert_open" and e.get("ack_url")]
    problems = []
    if not opens:
        problems.append("没有带 ack_url 的 alert_open")
    if problems:
        return False, "；".join(problems)
    url = with_by(opens[0]["ack_url"], "李四")  # 把 by 改成李四
    proc = None
    try:
        proc = start_server(state_path, None, SECRET, port)
        status, _ = get_url(url)
        if status != 200:
            return False, f"改 by 后点链接返回 {status}，应 200"
        st = read_state(state_path)
        dev = st["devices"]["10.0.0.9:162"]
        if not dev.get("acknowledged"):
            return False, "点链接后未置 acknowledged"
        if dev.get("acked_by") != "李四":
            return False, f"acked_by 应为 李四，实际 {dev.get('acked_by')}"
    finally:
        if proc is not None:
            proc.terminate()
    return True, "by 改成 李四 再点 → acked_by=李四（by 是自由字段）"


# ---------------------------------------------------------------- C8
def check_regression() -> tuple[bool, str]:
    problems = []
    for label, script in (("D20", CHECK_D20), ("D19", CHECK_D19), ("D18", CHECK_D18), ("D17", CHECK_D17)):
        proc = subprocess.run(
            [interpreter(), str(script)], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=make_env(), timeout=300,
        )
        if proc.returncode != 0:
            problems.append(f"{label} 回归挂了 rc={proc.returncode}\n" + tail(proc.stdout + proc.stderr))
    if problems:
        return False, "；".join(problems)
    return True, "D20 仍 8/8，D19 仍 8/8，D18 仍 8/8，D17 仍 9/9"


CHECKS = [
    ("C1", "无 roster 不带 by", check_no_roster_no_by),
    ("C2", "命中排班带 by", check_roster_hit_by),
    ("C3", "组链接带 by", check_group_link_has_by),
    ("C4", "排班不匹配不带 by", check_roster_mismatch_no_by),
    ("C5", "升级摘要 @值班人", check_escalate_digest_mentions_on_duty),
    ("C6", "端到端确认 by=张三", check_end_to_end_ack_by),
    ("C7", "by 可改写", check_mutable_by),
    ("C8", "D20/D19/D18/D17 回归", check_regression),
]


def main() -> int:
    print("=" * 64)
    print("D21 验收：值班排班（ack 链接 by= 默认值班人）")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")
    missing = [n for n in ("alert.py", "acktoken.py", "roster.py", "ack_server.py",
                            "check_d20.py", "check_d19.py", "check_d18.py", "check_d17.py")
               if not (ROOT / n).exists()]
    if missing:
        print(f"准备：[!] 缺文件：{', '.join(missing)}")
        return 1
    print("准备：OK")
    print(f"今天（用于排班命中）：{TODAY}")

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
    print("结论：全绿。值班排班跑通了。" if passed == total else "结论：还没全绿。")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
