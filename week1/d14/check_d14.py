"""D14 验收：并发批量扫描。

四项：
  B1  并发扫混合批次（OK/DEGRADED/FAILED）不抛异常，退出码按 FAILED 优先 = 1
  B2  报告覆盖全部设备（输出里 verdict 出现次数 >= 设备数）
  B3  并发速度：同一批设备，并发总耗时明显小于串行（证明没退化成排队）
  B5  单台模式（--port）行为不变（退出码 0）

check 在进程内 import probe：
  - 串行基准 = 顺序调用 _collect_one（不走线程池）
  - 并发     = 调 scan_config（内部线程池）
两者在同一进程、同一批设备下计时，最公平。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGENT = ROOT / "fake_agent.py"
ENTRY = ROOT / "probe.py"
VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"

PING_PACKET = bytes.fromhex(
    "302802010104067075626c6963a01b020344e2a8"
    "020100020100300e300c06082b060102010101000500"
)

N_UNREACHABLE = 6  # 不可达设备：串行会逐台等超时，并发只等一个


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


def tail(text: str, n: int = 800) -> str:
    body = (text or "").strip()
    return "\n".join(body.splitlines()[-12:])[:n] if body else "(空)"


def check_batch(ports: dict[str, int]) -> tuple[bool, str]:
    # 动态导入 d14 的 probe 模块（同进程，计时公平）
    sys.path.insert(0, str(ROOT))
    import importlib

    probe = importlib.import_module("probe")

    devs = [
        {"host": "127.0.0.1", "port": ports["normal"], "community": "public", "timeout": 0.5},
        {"host": "127.0.0.1", "port": ports["errs"], "community": "public", "timeout": 0.5},
    ]
    for i in range(N_UNREACHABLE):
        devs.append({"host": f"10.0.0.{100 + i}", "port": 161, "community": "secret", "timeout": 0.5})
    total = len(devs)

    # 串行基准：顺序调用 _collect_one，不走线程池
    t = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        _ = [probe._collect_one(d) for d in devs]
    serial = time.perf_counter() - t

    # 并发：走 scan_config（内部线程池）
    path = ROOT / "_check_devices.json"
    path.write_text(json.dumps({"devices": devs}, ensure_ascii=False, indent=2), encoding="utf-8")
    t = time.perf_counter()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        worst = probe.scan_config(str(path))
    concurrent = time.perf_counter() - t
    out = buf.getvalue()

    problems: list[str] = []
    # B1
    missing = [v for v in ("OK", "DEGRADED", "FAILED") if v not in out]
    if missing:
        problems.append(f"输出未出现 {missing}：{tail(out)}")
    if worst != 1:
        problems.append(f"B1 退出码应 1（FAILED 优先），实际 {worst}\n{tail(out)}")
    # B2
    verdict_hits = sum(out.count(v) for v in ("OK", "DEGRADED", "FAILED"))
    if verdict_hits < total:
        problems.append(f"B2 报告未覆盖全部设备：verdict 出现 {verdict_hits} 次 < 设备数 {total}")
    # B3：并发应明显快于串行（这里不可达设备会放大差异）
    if serial <= 0:
        problems.append("串行基准耗时为 0，无法比较")
    elif not (concurrent < serial * 0.7):
        problems.append(
            f"B3 并发不够快：并发 {concurrent:.1f}s，串行 {serial:.1f}s，"
            f"未达 < 串行×0.7（{serial * 0.7:.1f}s）。疑似退化成串行排队。"
        )
    if "汇总" not in out:
        problems.append(f"无汇总行：{tail(out)}")

    detail = f"串行 {serial:.1f}s vs 并发 {concurrent:.1f}s（{total} 台，含 {N_UNREACHABLE} 台不可达）"
    if problems:
        return False, "\n".join(problems) + "\n  " + detail
    return True, f"混合批次并发扫完，退出码 1，{detail}，提速明显"


def check_b5(normal_port: int) -> tuple[bool, str]:
    proc = subprocess.run(
        [interpreter(), str(ENTRY), "--port", str(normal_port)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=make_env(),
        timeout=30,
    )
    out = proc.stdout or ""
    if proc.returncode != 0:
        return False, f"单台模式退出码应是 0，实际 {proc.returncode}\n{tail(out + proc.stderr)}"
    if "OK" not in out:
        return False, f"单台模式输出里没有 OK：\n{tail(out)}"
    return True, "单台模式（--port）退出码 0，行为不变"


CHECKS = [
    ("B1-B3", "并发批量扫描：混合 verdict + 覆盖全部 + 比串行快", check_batch, True, True),
    ("B5", "单台模式（--port）行为不变", check_b5, True, True),
]


def main() -> int:
    print("=" * 64)
    print("D14 验收：并发批量扫描")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")

    missing = [n for n in ("probe.py", "ifwalk.py", "snmpio.py", "fake_agent.py") if not (ROOT / n).exists()]
    if missing:
        print(f"准备：[!] 缺文件：{', '.join(missing)}")
        return 1
    try:
        subprocess.run([interpreter(), "-c", "import pysnmp"], capture_output=True, env=make_env(), timeout=60, check=True)
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
        print("结论：还没全绿。先确认假设备起来了，再看是否退化成串行（耗时长）。")
    else:
        print("结论：全绿。并发批量扫描跑通了 —— 一批设备同时采，不再排队等超时。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
