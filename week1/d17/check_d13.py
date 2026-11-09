"""D13 验收：配置化批量扫描 + 退出码聚合。

五项：
  B1  读 devices.json 扫混合批次（OK / DEGRADED / FAILED）不抛异常
  B2  含一台不可达地址 → 该台 FAILED，整体退出码取最坏
  B3  部分失败那台 → DEGRADED，另两项照常采到
  B4  退出码聚合：FAILED 优先于 DEGRADED（整体退 1，不是 2）
  B5  单台模式（--port）行为不变（退出码 0）

check 自己拉起两台 fake_agent（正常 / 部分失败），第三台用不可达地址，
动态生成 _check_devices.json 后调 probe.py --config，跑完自动关 agent。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "probe.py"
AGENT = ROOT / "fake_agent.py"
VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"

# 一条真实的 SNMPv2c GET sysDescr 报文（真包），用来独立探活
PING_PACKET = bytes.fromhex(
    "302802010104067075626c6963a01b020344e2a8020100020100300e300c06082b060102010101000500"
)


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


def run_cli(*args: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
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


def tail(text: str, n: int = 800) -> str:
    body = (text or "").strip()
    return "\n".join(body.splitlines()[-12:])[:n] if body else "(空)"


def check_batch(ports: dict[str, int]) -> tuple[bool, str]:
    cfg = {
        "devices": [
            {"host": "127.0.0.1", "port": ports["normal"], "community": "public", "timeout": 0.5},
            {"host": "127.0.0.1", "port": ports["errs"], "community": "public", "timeout": 0.5},
            {"host": "10.0.0.99", "port": 161, "community": "secret", "timeout": 0.5},
        ]
    }
    path = ROOT / "_check_devices.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    proc = run_cli("--config", str(path), timeout=90)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode is None:
        return False, "子进程超时（可能被卡住）"

    problems: list[str] = []
    # B1：三种 verdict 都出现
    missing = [v for v in ("OK", "DEGRADED", "FAILED") if v not in out]
    if missing:
        problems.append(f"输出未出现 {missing}（说明某台没扫到）：{tail(out)}")
    # B4：退出码 = 1（FAILED 优先于 DEGRADED）
    if proc.returncode != 1:
        problems.append(
            f"B4 失败：整体退出码应 1（FAILED 优先），实际 {proc.returncode}。\n{tail(out)}"
        )
    if "汇总" not in out:
        problems.append(f"没有汇总行：{tail(out)}")

    if problems:
        return False, "\n".join(problems)
    return True, "混合批次扫完：OK/DEGRADED/FAILED 各出现，整体退出码 1（FAILED 优先），含汇总行"


def check_b5(normal_port: int) -> tuple[bool, str]:
    proc = run_cli("--port", str(normal_port), timeout=30)
    out = proc.stdout or ""
    if proc.returncode != 0:
        return False, f"单台模式退出码应是 0，实际 {proc.returncode}\n{tail(out + proc.stderr)}"
    if "OK" not in out:
        return False, f"单台模式输出里没有 OK：\n{tail(out)}"
    return True, "单台模式（--port）退出码 0，行为不变"


CHECKS = [
    ("B1-B4", "批量扫描：混合 verdict + 退出码聚合(FAILED 优先)", check_batch, True, True),
    ("B5", "单台模式（--port）行为不变", check_b5, True, True),
]


def main() -> int:
    print("=" * 64)
    print("D13 验收：配置化批量扫描 + 退出码聚合")
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
    print("准备：OK（probe.py / ifwalk.py / snmpio.py / fake_agent.py 齐全，pysnmp 可用）")

    took: set[int] = set()
    ports = {"normal": free_port(took), "errs": free_port(took)}
    agents: list[subprocess.Popen[bytes]] = [
        start_agent(ports["normal"]),
        start_agent(ports["errs"], "--gen-err", "--no-such-name"),
    ]
    alive = {name: wait_agent(port) for name, port in ports.items()}
    all_alive = all(alive.values())
    table = "　".join(f"{name} {ports[name]}{'' if alive[name] else ' [!]'}" for name in ports)
    print("假设备：" + table)

    passed = required_failed = 0
    try:
        for label, desc, fn, required, _need in CHECKS:
            print(f"\n{label}  {desc}")
            if not all_alive:
                print("    [skip] 假设备没起来")
                required_failed += 1
                continue
            try:
                good, message = fn(ports["normal"]) if label == "B5" else fn(ports)
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
        print("结论：还没全绿。先确认假设备起来了，再看 B4 的退出码是否按 FAILED 优先。")
    else:
        print("结论：全绿。配置化批量扫描跑通了 —— D14 把 for 循环换成并发即可。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
