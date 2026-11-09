"""D19 验收：ack 闭环 + 告警生命周期看板。

C1  ack 链接签名：合法可验、伪造被拒
C2  通知内嵌确认链接（alert_open / group_alert 各自带 device/group 链接）
C3  看板渲染 ALERT 设备：持续时长 / 确认人 / 升级级别 / 所属组
C4  闭环端到端：alert 发链接 → 点链接 → 状态被确认（acknowledged=True）
C5  伪造签名点链接：服务端 403，状态不被改动
C6  组确认链接：一次点完组内所有设备
C7  看板反映组状态：组 ALERT 进组行；组恢复后退出组行，但组内仍坏设备保留设备行
C8  回归：D18（8/8）+ D17（9/9）

说明：C4/C5/C6 真的起一个 HTTP 服务（ThreadingHTTPServer，临时端口）发请求，
不走 mock，验证「链接→服务端→写回状态」整条链路。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALERT = ROOT / "alert.py"
ACK = ROOT / "ack.py"
CHECK_D18 = ROOT / "check_d18.py"
CHECK_D17 = ROOT / "check_d17.py"
VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"
ACK_PORT_BASE = 8740
SECRET = "d19-test-secret"

(ROOT / "out").mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="_d19check_", dir=str(ROOT / "out")))

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


def write_state(path: Path, devices: dict, groups: dict | None = None) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": 1, "updated_at": now,
        "devices": devices, "groups": groups or {}, "pending": [],
    }, ensure_ascii=False), encoding="utf-8")


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


# ---- HTTP 闭环服务 ----
def start_server(state_path: Path, groups_path: Path, secret: str, port: int):
    import ack_server
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ack_server.AckHandler)
    httpd.state_path = str(state_path)
    httpd.groups_path = str(groups_path) if groups_path else None
    httpd.secret = secret
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def http_get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- C1
def check_token() -> tuple[bool, str]:
    import acktoken
    secret = "s3cr3t"
    label = "10.0.0.9:161"
    sig = acktoken.sign_device(secret, label)
    problems = []
    if not acktoken.verify_device(secret, label, sig):
        problems.append("合法签名校验失败")
    if acktoken.verify_device(secret, label, "deadbeef"):
        problems.append("伪造签名居然通过")
    if acktoken.verify_device("别的密钥", label, sig):
        problems.append("换密钥仍能验过（密钥没生效）")
    if acktoken.verify_device(secret, label, None):
        problems.append("空签名通过")
    # 组签名独立命名空间
    gsig = acktoken.sign_group(secret, "机房A")
    if not acktoken.verify_group(secret, "机房A", gsig):
        problems.append("组签名校验失败")
    if acktoken.verify_group(secret, "机房A", sig):
        problems.append("组签名被设备签名冒充")
    if problems:
        return False, "；".join(problems)
    return True, "device/group 签名独立命名空间，合法可验、伪造/换密钥/空均拒"


# ---------------------------------------------------------------- C2
def check_notify_embeds_link() -> tuple[bool, str]:
    report_dir, state_path, log_path = paths("c2")
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.40", 161, "FAILED")], minutes_ago=ago)
    base = "http://127.0.0.1:8731"
    p = run_alert(report_dir, state_path, log_path, "--ack-base-url", base)
    log = read_log(log_path)
    opens = [e for e in log if e.get("kind") == "alert_open"]
    problems = []
    if not opens:
        problems.append("没有 alert_open（场景没造出来）")
    else:
        urls = [e.get("ack_url") for e in opens if e.get("ack_url")]
        if not urls:
            problems.append("alert_open 没带 ack_url")
        elif not all("device=" in u and "/ack?" in u for u in urls):
            problems.append("ack_url 格式不对（应含 /ack?device=）")
    if problems:
        return False, "；".join(problems) + "\n" + tail((p.stdout or "") + (p.stderr or ""))
    return True, "alert_open 内嵌设备确认链接（file 渠道也写进日志 JSON）"


# ---------------------------------------------------------------- C3
def check_board_renders() -> tuple[bool, str]:
    _, state_path, _ = paths("c3")
    since = (datetime.now() - timedelta(minutes=23)).isoformat(timespec="seconds")
    write_state(state_path, {
        "10.0.0.9:161": {
            "status": "ALERT", "since": since, "stale": False, "consecutive_bad": 3,
            "consecutive_ok": 0, "last_seen": since, "last_notified_at": since,
            "acknowledged": True, "acked_by": "alice", "acked_at": since,
            "escalation_level": 1, "last_escalated_at": since,
        },
    })
    proc = subprocess.run(
        [interpreter(), str(ROOT / "board.py"), "--state", str(state_path), "--json"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=make_env(), timeout=60,
    )
    try:
        data = json.loads(proc.stdout)
    except (ValueError, json.JSONDecodeError):
        return False, "board --json 不是合法 JSON\n" + tail(proc.stdout + proc.stderr)
    rows = data.get("devices", [])
    problems = []
    if len(rows) != 1:
        problems.append(f"应 1 台 ALERT，实际 {len(rows)}")
    else:
        r = rows[0]
        if r.get("acked_by") != "alice":
            problems.append("确认人没渲染")
        if r.get("escalation_level") != 1:
            problems.append("升级级别没渲染")
        if "23" not in str(r.get("age", "")) and "0.4" not in str(r.get("age", "")):
            problems.append(f"持续时长没渲染（age={r.get('age')}）")
    if problems:
        return False, "；".join(problems) + "\n" + tail(proc.stdout)
    return True, "看板渲染设备：持续/确认人/升级级别/组 齐全"


# ---------------------------------------------------------------- C4
def check_closure_e2e() -> tuple[bool, str]:
    name = "c4"
    report_dir, state_path, log_path = paths(name)
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.40", 161, "FAILED")], minutes_ago=ago)
    base = f"http://127.0.0.1:{ACK_PORT_BASE + 4}"
    run_alert(report_dir, state_path, log_path, "--ack-base-url", base, "--ack-secret", SECRET)

    log = read_log(log_path)
    opens = [e for e in log if e.get("kind") == "alert_open" and e.get("ack_url")]
    problems = []
    if not opens:
        problems.append("没有带 ack_url 的 alert_open")
        return False, "；".join(problems)
    url = opens[0]["ack_url"]

    server = start_server(state_path, Path(TMP / name / "groups.json"), SECRET, ACK_PORT_BASE + 4)
    code, _body = http_get(url)
    server.shutdown()
    state = read_state(state_path)
    dev = state["devices"].get("10.0.0.40:161", {})
    if code != 200:
        problems.append(f"点链接返回 {code}（应 200）")
    if not dev.get("acknowledged"):
        problems.append("点链接后状态未被确认")
    if dev.get("acked_by") == "unknown":
        problems.append("确认人未记录")
    if problems:
        return False, "；".join(problems)
    return True, "闭环端到端：alert 发链接→点链接→状态 acknowledged=True"


# ---------------------------------------------------------------- C5
def check_forged_rejected() -> tuple[bool, str]:
    name = "c5"
    report_dir, state_path, log_path = paths(name)
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, [device("10.0.0.40", 161, "FAILED")], minutes_ago=ago)
    run_alert(report_dir, state_path, log_path)
    label = "10.0.0.40:161"
    server = start_server(state_path, Path(TMP / name / "groups.json"), "secret", ACK_PORT_BASE + 5)
    # 伪造签名
    url = f"http://127.0.0.1:{ACK_PORT_BASE + 5}/ack?device={label}&sig=forged"
    code, _body = http_get(url)
    server.shutdown()
    state = read_state(state_path)
    problems = []
    if code != 403:
        problems.append(f"伪造签名应 403，实际 {code}")
    if state["devices"].get(label, {}).get("acknowledged"):
        problems.append("伪造签名居然改了状态")
    if problems:
        return False, "；".join(problems)
    return True, "伪造签名点链接：服务端 403，状态不被改动"


# ---------------------------------------------------------------- C6
def check_group_link() -> tuple[bool, str]:
    name = "c6"
    report_dir, state_path, log_path = paths(name)
    devs = [device("10.0.0.9", 161, "FAILED"), device("10.0.0.9", 162, "FAILED"), device("10.0.0.9", 163, "FAILED")]
    for seq, ago in enumerate((3.0, 2.0, 1.0), start=1):
        write_report(report_dir, seq, devs, minutes_ago=ago)
    gpath = write_groups(TMP / name, {"机房A": ["10.0.0.9:161", "10.0.0.9:162", "10.0.0.9:163"]})
    base = f"http://127.0.0.1:{ACK_PORT_BASE + 6}"
    run_alert(report_dir, state_path, log_path, "--groups", str(gpath), "--ack-base-url", base,
              "--ack-secret", SECRET, "--escalate-channel", "file")

    log = read_log(log_path)
    groups = [e for e in log if e.get("kind") == "group_alert" and e.get("ack_url")]
    problems = []
    if not groups:
        problems.append("没有带 ack_url 的 group_alert")
        return False, "；".join(problems)
    url = groups[0]["ack_url"]
    server = start_server(state_path, gpath, SECRET, ACK_PORT_BASE + 6)
    code, _body = http_get(url)
    server.shutdown()
    state = read_state(state_path)
    acked = sum(1 for m in ("10.0.0.9:161", "10.0.0.9:162", "10.0.0.9:163")
                if state["devices"].get(m, {}).get("acknowledged"))
    if code != 200:
        problems.append(f"组链接返回 {code}")
    if acked != 3:
        problems.append(f"组链接只确认 {acked}/3 台")
    if problems:
        return False, "；".join(problems)
    return True, "组确认链接：一次点完组内 3 台"


# ---------------------------------------------------------------- C7
def check_board_group() -> tuple[bool, str]:
    name = "c7"
    _, state_path, _ = paths(name)
    gpath = write_groups(TMP / name, {"机房A": ["10.0.0.9:161", "10.0.0.9:162"]})
    since = (datetime.now() - timedelta(minutes=12)).isoformat(timespec="seconds")
    # 组 ALERT，且组内 1 台仍坏
    write_state(state_path, {
        "10.0.0.9:161": {"status": "ALERT", "since": since, "stale": False,
                          "consecutive_bad": 3, "consecutive_ok": 0, "last_seen": since,
                          "acknowledged": False, "acked_by": None, "acked_at": None,
                          "escalation_level": 0, "last_escalated_at": None},
    }, groups={"机房A": {"status": "ALERT", "since": since}})

    def board_rows():
        proc = subprocess.run(
            [interpreter(), str(ROOT / "board.py"), "--state", str(state_path),
             "--groups", str(gpath), "--json"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=make_env(), timeout=60,
        )
        return json.loads(proc.stdout)

    data = board_rows()
    problems = []
    if not data["groups"]:
        problems.append("组 ALERT 时看板应有组行")
    if len(data["devices"]) != 1:
        problems.append(f"组 ALERT 时设备行应为 1，实际 {len(data['devices'])}")

    # 组恢复
    state = read_state(state_path)
    state["groups"]["机房A"]["status"] = "OK"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    data2 = board_rows()
    if data2["groups"]:
        problems.append("组恢复后看板仍显示组行")
    if len(data2["devices"]) != 1:
        problems.append("组恢复不该把组内仍坏设备从设备行抹掉")
    if problems:
        return False, "；".join(problems)
    return True, "看板反映组状态：组 ALERT 进组行，组恢复退出但仍坏设备保留设备行"


# ---------------------------------------------------------------- C8
def check_regression() -> tuple[bool, str]:
    problems = []
    for label, script in (("D18", CHECK_D18), ("D17", CHECK_D17)):
        proc = subprocess.run(
            [interpreter(), str(script)], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=make_env(), timeout=240,
        )
        if proc.returncode != 0:
            problems.append(f"{label} 回归挂了 rc={proc.returncode}\n" + tail(proc.stdout + proc.stderr))
    if problems:
        return False, "；".join(problems)
    return True, "D18 仍 8/8，D17 仍 9/9"


CHECKS = [
    ("C1", "ack 链接签名", check_token),
    ("C2", "通知内嵌确认链接", check_notify_embeds_link),
    ("C3", "看板渲染 ALERT 设备", check_board_renders),
    ("C4", "闭环端到端", check_closure_e2e),
    ("C5", "伪造签名被拒", check_forged_rejected),
    ("C6", "组确认链接", check_group_link),
    ("C7", "看板反映组状态", check_board_group),
    ("C8", "D18/D17 回归", check_regression),
]


def main() -> int:
    print("=" * 64)
    print("D19 验收：ack 闭环 + 告警生命周期看板")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")
    missing = [n for n in ("alert.py", "alerts.py", "state.py", "notify.py",
                            "ack.py", "acktoken.py", "ack_server.py", "board.py")
               if not (ROOT / n).exists()]
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
    print("结论：全绿。ack 闭环与看板跑通了。" if passed == total else "结论：还没全绿。")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
