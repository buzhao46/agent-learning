"""D15 验收：结果落盘 + 增量巡检。

B1  全量扫描生成 JSON 报告，结构完整（schema / generated_at / summary / devices），设备数 == 配置数
B2  报告内容与 verdict 一致（summary 计数、每台设备的 verdict / failures 带 kind / hint）
B3  --incremental 只重扫上次非 OK 的设备，且合并后报告仍是完整设备数
B4  全部 OK 时增量跳过，退出码 0
B5  单台模式（--port）行为不变

说明：本脚本验证的是 D15 的目标（report.py + probe.py 的 --report-dir / --incremental）。
若只复制了代码、尚未实现上述功能，B1-B4 会失败，这是预期结果。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGENT = ROOT / "fake_agent.py"
ENTRY = ROOT / "probe.py"
VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"

PING_PACKET = bytes.fromhex(
    "302802010104067075626c6963a01b020344e2a8020100020100300e300c06082b060102010101000500"
)

# 所有临时产物都放在 out/ 下（out/ 已在 .gitignore，不会被误提交）。
# 用 mkdtemp 每次生成唯一目录，避免依赖递归删除（部分环境会拦截 shutil.rmtree）。
(ROOT / "out").mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="_d15check_", dir=str(ROOT / "out")))
DEVICES_PATH = TMP / "devices.json"
DEVICES_OK_PATH = TMP / "devices_ok.json"
REPORT_DIR = TMP / "reports"
REPORT_DIR_OK = TMP / "reports_ok"


def interpreter() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def make_env() -> dict[str, str]:
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e["PYTHONUTF8"] = "1"
    e["PYTHONPATH"] = str(ROOT) + os.pathsep + e.get("PYTHONPATH", "")
    return e


def free_port(taken: set[int]) -> int:
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        if port not in taken:
            taken.add(port)
            return port
    raise RuntimeError("找不到空闲端口")


def snmp_ping(port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(PING_PACKET, ("127.0.0.1", port))
            return bool(s.recvfrom(4096)[0])
        except OSError:
            return False


def start_agent(port: int, *flags: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [interpreter(), str(AGENT), "--port", str(port), *flags],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=make_env(),
    )


def wait_agent(port: int, seconds: float = 8.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if snmp_ping(port):
            return True
        time.sleep(0.2)
    return False


def tail(text: str, n: int = 900) -> str:
    body = (text or "").strip()
    return "\n".join(body.splitlines()[-14:])[:n] if body else "(空)"


def run_probe(*args: str, timeout: float = 90) -> subprocess.CompletedProcess[str]:
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


def write_devices(path: Path, devices: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"devices": devices}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def latest_report(directory: Path) -> tuple[Path | None, dict | None]:
    files = sorted(directory.glob("scan_*.json")) if directory.exists() else []
    if not files:
        return None, None
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return latest, json.loads(latest.read_text(encoding="utf-8"))


def check_report(ports: dict[str, int]) -> tuple[bool, str]:
    # 3 台：OK / DEGRADED / FAILED
    devices = [
        {"host": "127.0.0.1", "port": ports["normal"], "community": "public", "timeout": 0.5},
        {"host": "127.0.0.1", "port": ports["errs"], "community": "public", "timeout": 0.5},
        {"host": "10.0.0.200", "port": 161, "community": "secret", "timeout": 0.5},
    ]
    write_devices(DEVICES_PATH, devices)

    proc = run_probe("--config", str(DEVICES_PATH), "--report-dir", str(REPORT_DIR))
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 1:
        return False, f"全量扫描退出码应 1（含 FAILED），实际 {proc.returncode}\n{tail(out)}"

    path, report = latest_report(REPORT_DIR)
    if report is None:
        return False, f"未生成 scan_*.json 报告（--report-dir 未生效？）\n{tail(out)}"

    problems: list[str] = []
    for key in ("schema", "generated_at", "summary", "devices"):
        if key not in report:
            problems.append(f"报告缺字段 {key}")
    if not problems:
        summary = report["summary"]
        for key in ("total", "ok", "degraded", "failed", "exit_code"):
            if key not in summary:
                problems.append(f"summary 缺字段 {key}")
        if summary.get("total") != len(devices):
            problems.append(f"summary.total={summary.get('total')} != 设备数 {len(devices)}")
        if len(report["devices"]) != len(devices):
            problems.append(f"devices 数 {len(report['devices'])} != 配置数 {len(devices)}")
        verdicts = [d.get("verdict") for d in report["devices"]]
        if summary.get("ok") != verdicts.count("OK"):
            problems.append("summary.ok 与 devices 中 OK 数不符")
        if summary.get("degraded") != verdicts.count("DEGRADED"):
            problems.append("summary.degraded 与 devices 中 DEGRADED 数不符")
        if summary.get("failed") != verdicts.count("FAILED"):
            problems.append("summary.failed 与 devices 中 FAILED 数不符")
        for d in report["devices"]:
            for key in ("host", "verdict", "exit_code", "values", "failures"):
                if key not in d:
                    problems.append(f"设备项缺字段 {key}（{d.get('host')}）")
            for f in d.get("failures", []):
                for key in ("stage", "kind", "detail", "hint"):
                    if key not in f:
                        problems.append(f"失败项缺字段 {key}（{d.get('host')}）")

    if problems:
        return False, "；".join(problems) + "\n" + tail(out)
    return True, f"报告 {path.name} 结构完整，{len(report['devices'])} 台，summary 计数一致"


def check_incremental(ports: dict[str, int]) -> tuple[bool, str]:
    # 依赖 check_report 生成的报告（含 DEGRADED + FAILED 两台非 OK）
    _, before = latest_report(REPORT_DIR)
    if before is None:
        return False, "缺少上一份报告，无法验证增量（需先通过 B1-B2）"
    non_ok = [d for d in before["devices"] if d["verdict"] != "OK"]
    if not non_ok:
        return False, "上一份报告没有非 OK 设备，无法验证增量"

    proc = run_probe(
        "--config",
        str(DEVICES_PATH),
        "--report-dir",
        str(REPORT_DIR),
        "--incremental",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if "增量巡检" not in out:
        return False, f"输出里没有『增量巡检：重扫上次非 OK 的 N 台』\n{tail(out)}"
    if f"{len(non_ok)} 台" not in out:
        return False, f"增量重扫台数不等于非 OK 数 {len(non_ok)}\n{tail(out)}"

    _, after = latest_report(REPORT_DIR)
    if after is None:
        return False, "增量后未生成报告"
    if len(after["devices"]) != len(before["devices"]):
        return False, (
            f"增量后报告设备数 {len(after['devices'])} != 原 {len(before['devices'])}；"
            "疑似只写了重扫子集（应合并回完整报告）"
        )
    return True, f"增量只重扫 {len(non_ok)} 台非 OK，合并后仍 {len(after['devices'])} 台"


def check_incremental_all_ok(ports: dict[str, int]) -> tuple[bool, str]:
    write_devices(
        DEVICES_OK_PATH,
        [
            {"host": "127.0.0.1", "port": ports["normal"], "community": "public", "timeout": 0.5},
        ],
    )
    first = run_probe("--config", str(DEVICES_OK_PATH), "--report-dir", str(REPORT_DIR_OK))
    if first.returncode != 0:
        return (
            False,
            f"全 OK 全量扫描退出码应 0，实际 {first.returncode}\n{tail(first.stdout + first.stderr)}",
        )

    second = run_probe(
        "--config",
        str(DEVICES_OK_PATH),
        "--report-dir",
        str(REPORT_DIR_OK),
        "--incremental",
    )
    out = (second.stdout or "") + (second.stderr or "")
    if second.returncode != 0:
        return False, f"全 OK 增量退出码应 0，实际 {second.returncode}\n{tail(out)}"
    if "跳过巡检" not in out:
        return False, f"全 OK 增量应提示跳过巡检\n{tail(out)}"
    return True, "全 OK 时增量跳过，退出码 0"


def check_single(ports: dict[str, int]) -> tuple[bool, str]:
    proc = run_probe("--port", str(ports["normal"]))
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, f"单台模式退出码应 0，实际 {proc.returncode}\n{tail(out)}"
    if "OK" not in out:
        return False, f"单台输出没有 OK\n{tail(out)}"
    return True, "单台模式（--port）退出码 0，行为不变"


CHECKS = [
    ("B1-B2", "全量落盘 + 报告结构与内容", check_report, True),
    ("B3", "增量只重扫非 OK 并合并完整", check_incremental, True),
    ("B4", "全 OK 时增量跳过", check_incremental_all_ok, True),
    ("B5", "单台模式行为不变", check_single, True),
]


def main() -> int:
    print("=" * 64)
    print("D15 验收：结果落盘 + 增量巡检")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")

    missing = [
        n
        for n in ("probe.py", "ifwalk.py", "snmpio.py", "fake_agent.py")
        if not (ROOT / n).exists()
    ]
    if missing:
        print(f"准备：[!] 缺文件：{', '.join(missing)}")
        return 1
    try:
        subprocess.run(
            [interpreter(), "-c", "import pysnmp"],
            capture_output=True,
            env=make_env(),
            timeout=60,
            check=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"准备：[!] 解释器里没有 pysnmp：{exc}")
        return 1
    print("准备：OK")

    took: set[int] = set()
    ports = {"normal": free_port(took), "errs": free_port(took)}
    agents: list[subprocess.Popen[bytes]] = [
        start_agent(ports["normal"]),
        start_agent(ports["errs"], "--gen-err", "--no-such-name"),
    ]
    alive = {name: wait_agent(port) for name, port in ports.items()}
    all_alive = all(alive.values())
    print(
        "假设备："
        + "　".join(f"{name} {ports[name]}{'' if alive[name] else ' [!]'}" for name in ports)
    )

    passed = required_failed = 0
    try:
        for label, desc, fn, required in CHECKS:
            print(f"\n{label}  {desc}")
            if not all_alive:
                print("    [skip] 假设备没起来")
                required_failed += 1
                continue
            try:
                good, message = fn(ports)
            except Exception as exc:  # noqa: BLE001
                good, message = False, f"验收时抛异常：{type(exc).__name__}: {exc}"
            print(f"    [{'OK' if good else 'NG'}] {message}")
            if good:
                passed += 1
            elif required:
                required_failed += 1
    finally:
        for agent in agents:
            agent.terminate()
            try:
                agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()

    total = len(CHECKS)
    print("\n" + "-" * 64)
    print(f"通过 {passed}/{total}　必做失败 {required_failed}")
    if required_failed:
        print(
            "结论：还没全绿。若只完成「复制 + 验收脚本」、尚未实现 report.py 与 --report-dir/--incremental，这是预期结果。"
        )
    else:
        print("结论：全绿。结果落盘 + 增量巡检跑通了。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
