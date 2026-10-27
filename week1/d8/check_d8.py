"""D8 验收：第一次真采集（pysnmp getCmd）。

七项：
  C1  snmp_get 能读回 sysDescr
  C2  换个 OID 也能读（sysName）—— OID 是参数，不是写死的
  C3  fmt_ticks 单位换算正确（1/100 秒）
  C4  probe_health 三项齐全，uptime 已格式化
  C5  超时 → ProbeError，消息含 host 和端口，且不拖太久
  C6  不存在的 OID → ProbeError（不能把 NoSuchObject 当正常值）
  C7  [加练] python getprobe.py --port ... 能打印可读结果

验收会自己拉起 fake_agent.py（一个本机假设备），跑完自动关掉。
你不用手动开窗口，端口也是自动挑的空闲端口。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "getprobe.py"
AGENT = ROOT / "fake_agent.py"

VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"

# 一条真实的 SNMPv2c GET sysDescr 报文（从 pysnmp 抓下来的真包）。
# 用来独立探活：不依赖学员代码，只确认假设备真的在应答。
PING_PACKET = bytes.fromhex(
    "302802010104067075626c6963a01b020344e2a8"
    "020100020100300e300c06082b060102010101000500"
)


def interpreter() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def make_env() -> dict[str, str]:
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e["PYTHONUTF8"] = "1"
    e["PYTHONPATH"] = str(ROOT) + os.pathsep + e.get("PYTHONPATH", "")
    return e


def free_port() -> int:
    """跟系统要一个当前没人用的端口，立刻释放。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def snmp_ping(port: int, timeout: float = 0.4) -> bool:
    """发一条真报文，收到任何回包就算假设备活着。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(PING_PACKET, ("127.0.0.1", port))
            s.recvfrom(4096)
            return True
        except OSError:
            return False


def wait_agent(port: int, seconds: float = 6.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if snmp_ping(port):
            return True
        time.sleep(0.15)
    return False


# 在子进程里调学员的函数，把结果用 JSON 打回来。
# 为什么要子进程：她写崩了只会让这一项红，不会把验收脚本一起带崩；
# 而且每次都是全新的解释器，不会残留 _ENGINE 之类的状态。
PROBE = r"""
import json, sys, time
import getprobe as g

host = "127.0.0.1"
port = int(sys.argv[1])
dead = int(sys.argv[2])
action = sys.argv[3]

def emit(payload):
    print("###" + json.dumps(payload, ensure_ascii=False))

start = time.time()
try:
    if action == "sysdescr":
        out = g.snmp_get(host, g.OID_SYSDESCR, port=port, timeout=1.0)
    elif action == "sysname":
        out = g.snmp_get(host, g.OID_SYSNAME, port=port, timeout=1.0)
    elif action == "fmt":
        out = {"a": g.fmt_ticks(12345678), "b": g.fmt_ticks(6000)}
    elif action == "health":
        out = g.probe_health(host, port=port, timeout=1.0)
    elif action == "nosuch":
        out = g.snmp_get(host, "1.3.6.1.2.1.1.9.0", port=port, timeout=1.0)
    elif action == "timeout":
        out = g.snmp_get(host, g.OID_SYSDESCR, port=dead, timeout=0.5)
    else:
        raise SystemExit("未知 action: " + action)
    emit({"ok": True, "value": out, "elapsed": time.time() - start})
except BaseException as exc:
    emit({
        "ok": False,
        "type": type(exc).__name__,
        "is_probe_error": isinstance(exc, g.ProbeError),
        "msg": str(exc),
        "elapsed": time.time() - start,
    })
"""


def probe(action: str, port: int, dead: int = 0, timeout: int = 40) -> dict[str, object]:
    proc = subprocess.run(
        [interpreter(), "-c", PROBE, str(port), str(dead or port), action],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=make_env(),
        timeout=timeout,
    )
    for line in (proc.stdout or "").splitlines():
        if line.startswith("###"):
            return json.loads(line[3:])
    blob = (proc.stdout or "") + (proc.stderr or "")
    return {
        "ok": False,
        "type": "验收脚本没拿到结果",
        "is_probe_error": False,
        "msg": "\n".join(blob.strip().splitlines()[-6:]) or "(空输出)",
        "elapsed": 0.0,
    }


def tail(text: str, n: int = 500) -> str:
    body = (text or "").strip()
    return "\n".join(body.splitlines()[-8:])[:n] if body else "(空)"


def describe(result: dict[str, object]) -> str:
    if result.get("ok"):
        return f"返回 {result.get('value')!r}"
    return f"抛了 {result.get('type')}: {result.get('msg')}"


# ============================================================ 七项检查
def check_c1(port: int, dead: int) -> tuple[bool, str]:
    result = probe("sysdescr", port, dead)
    if not result.get("ok"):
        return False, (
            f"snmp_get 没读回 sysDescr。{describe(result)}\n"
            "先确认：getCmd 的四个返回值你都解开了吗？error_indication 是 None 吗？"
        )
    value = str(result["value"])
    if "Huawei" not in value:
        return False, (
            f"读回来的值不像设备型号：{value!r}\n"
            "是不是把 (OID, 值) 整个元组当值返回了？要取「值」再 prettyPrint()。"
        )
    return True, f"sysDescr = {value[:60]}"


def check_c2(port: int, dead: int) -> tuple[bool, str]:
    result = probe("sysname", port, dead)
    if not result.get("ok"):
        return False, f"换 OID 读 sysName 失败。{describe(result)}"
    value = str(result["value"]).strip()
    if value != "core-sw-01":
        return False, (
            f"sysName 应为 'core-sw-01'，实际 {value!r}\n"
            "如果你把 OID 写死在 snmp_get 里，这里就会一直返回 sysDescr 的内容。"
        )
    return True, "sysName = core-sw-01（OID 确实是参数）"


def check_c3(port: int, dead: int) -> tuple[bool, str]:
    result = probe("fmt", port, dead)
    if not result.get("ok"):
        return False, f"fmt_ticks 调用失败。{describe(result)}"
    got = result["value"]
    assert isinstance(got, dict)
    want = {"a": "1 day, 10:17:36.78", "b": "00:01:00.00"}
    wrong = {k: got.get(k) for k in want if got.get(k) != want[k]}
    if wrong:
        hint = (
            "12345678 ticks → 123456.78 秒 → 1 天 10:17:36.78\n"
            "6000 ticks → 60 秒 → 00:01:00.00\n"
            "两个都错：八成忘了 /100（单位是百分之一秒）；\n"
            "只错 b：不足一天时不该写天数，且时分秒要两位补零。"
        )
        return False, f"格式不对：{got}\n应为 {want}\n{hint}"
    return True, "12345678 → 1 day, 10:17:36.78；6000 → 00:01:00.00"


def check_c4(port: int, dead: int) -> tuple[bool, str]:
    result = probe("health", port, dead)
    if not result.get("ok"):
        return False, f"probe_health 失败。{describe(result)}"
    got = result["value"]
    if not isinstance(got, dict):
        return False, f"probe_health 应返回 dict，实际返回 {type(got).__name__}"
    need = {"sys_descr": "Huawei", "sys_name": "core-sw-01", "uptime": "1 day, 10:17:36.78"}
    for key, expect in need.items():
        if key not in got:
            return False, f"返回的字典缺少键 {key!r}，实际有 {list(got)}"
        if expect not in str(got[key]):
            return False, (
                f"{key} 不对：{got[key]!r}，应包含 {expect!r}\n"
                "（uptime 必须是 fmt_ticks 处理过的，不是原始 ticks 数字）"
            )
    return True, f"sys_descr / sys_name / uptime 三项齐全：{got['uptime']}"


def check_c5(port: int, dead: int) -> tuple[bool, str]:
    result = probe("timeout", port, dead)
    if result.get("ok"):
        return False, (
            f"往一个没人监听的端口发 GET，居然「成功」返回了 {result.get('value')!r}。\n"
            "超时的时候 error_indication 不是 None，必须当成失败抛出去。"
        )
    if not result.get("is_probe_error"):
        return False, (
            f"超时抛的是 {result.get('type')}，不是 ProbeError。{result.get('msg')}\n"
            "新手常在这里抛 RuntimeError 或直接 raise 原始异常——"
            "调用方就没办法统一捕获了。"
        )
    msg = str(result.get("msg") or "")
    if "127.0.0.1" not in msg or str(dead) not in msg:
        return False, (
            f"异常消息里缺设备标识：{msg!r}\n"
            f"应同时含 host 和 port（期望包含 '127.0.0.1' 和 '{dead}'）。\n"
            "巡检几十台设备时，'采集超时' 这四个字帮不上任何忙。"
        )
    elapsed = float(result.get("elapsed") or 0)
    if elapsed > 3.0:
        return False, (
            f"超时用了 {elapsed:.1f} 秒才返回（timeout 传的是 0.5）。\n"
            "多半是 UdpTransportTarget 没写 retries=0：默认 retries=5，会把等待放大 6 倍。"
        )
    return True, f"{elapsed:.2f}s 内抛 ProbeError，消息含 host 与端口"


def check_c6(port: int, dead: int) -> tuple[bool, str]:
    result = probe("nosuch", port, dead)
    if result.get("ok"):
        value = str(result.get("value"))
        return False, (
            f"读一个不存在的 OID，返回了「成功」：{value!r}\n"
            "这就是今天最隐蔽的坑：设备回的 error_status 是 0（没报错），\n"
            "但 varbind 里塞的不是字符串，而是 NoSuchObject 标记。\n"
            "它的 prettyPrint() 恰好是一句像模像样的英文，很容易被当成正常值。\n"
            "判据是值的类型：isinstance(值, NoSuchObject) 或 type(值).__name__。"
        )
    if not result.get("is_probe_error"):
        return False, f"抛的是 {result.get('type')} 而不是 ProbeError：{result.get('msg')}"
    return True, "不存在的 OID 被识别成失败，没有把 NoSuchObject 当值返回"


def check_c7(port: int, dead: int) -> tuple[bool, str]:
    proc = subprocess.run(
        [interpreter(), "getprobe.py", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=make_env(),
        timeout=60,
    )
    if proc.returncode != 0:
        return False, f"退出码 {proc.returncode}\nstderr：\n{tail(proc.stderr)}"
    out = proc.stdout or ""
    missing = [s for s in ("Huawei", "core-sw-01", "day") if s not in out]
    if missing:
        return False, f"输出里看不到 {missing}\n实际输出：\n{tail(out)}"
    return True, "命令行走通，三行都打出来了"


CHECKS: list[tuple[str, str, object, bool]] = [
    ("C1", "snmp_get 读回 sysDescr", check_c1, True),
    ("C2", "换 OID 读 sysName（OID 是参数）", check_c2, True),
    ("C3", "fmt_ticks 单位换算（1/100 秒）", check_c3, True),
    ("C4", "probe_health 三项齐全", check_c4, True),
    ("C5", "超时 → ProbeError，消息可定位", check_c5, True),
    ("C6", "不存在的 OID → ProbeError", check_c6, True),
    ("C7", "python getprobe.py --port ...", check_c7, False),
]


def ready() -> tuple[bool, str]:
    missing = [n for n in ("getprobe.py", "fake_agent.py") if not (ROOT / n).exists()]
    if missing:
        return False, f"缺文件：{', '.join(missing)}"
    src = ENTRY.read_text(encoding="utf-8")
    if re.search(r"^\s*raise NotImplementedError", src, re.M):
        return False, "getprobe.py 里还有 raise NotImplementedError —— 三个函数还没写完"
    try:
        subprocess.run(
            [interpreter(), "-c", "import pysnmp"],
            capture_output=True,
            env=make_env(),
            timeout=60,
            check=True,
        )
    except Exception:  # noqa: BLE001
        return False, "装 pysnmp 的解释器里 import pysnmp 失败 —— 确认用的是 week1/.venv"
    return True, "getprobe.py 已无占位，pysnmp 可导入"


def main() -> int:
    print("=" * 64)
    print("D8 验收：第一次真采集（pysnmp getCmd）")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")

    ok_ready, info = ready()
    print(f"准备：{info}" if ok_ready else f"准备：⚠️  {info}")

    port = free_port()
    dead = free_port()
    agent: subprocess.Popen[bytes] | None = None
    alive = False
    if ok_ready:
        agent = subprocess.Popen(
            [interpreter(), "fake_agent.py", "--port", str(port)],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=make_env(),
        )
        alive = wait_agent(port)
        print(f"假设备：127.0.0.1:{port}" + ("" if alive else "  ⚠️  没起来"))

    passed = required_failed = optional_failed = 0
    try:
        for label, desc, fn, required in CHECKS:
            print(f"\n{label}  {desc}" + ("" if required else "　[加练]"))
            if not ok_ready:
                print("    ⏭  跳过（准备检查没过）")
                if required:
                    required_failed += 1
                else:
                    optional_failed += 1
                continue
            if not alive:
                print("    ⏭  跳过（假设备没起来，可能是端口被占或 fake_agent.py 被改过）")
                if required:
                    required_failed += 1
                else:
                    optional_failed += 1
                continue
            try:
                good, message = fn(port, dead)  # type: ignore[operator]
            except Exception as exc:  # noqa: BLE001 - 验收脚本自己绝不能崩
                good, message = False, f"验收时抛异常：{type(exc).__name__}: {exc}"
            print(f"    {'✅' if good else '❌'} {message}")
            if good:
                passed += 1
            elif required:
                required_failed += 1
            else:
                optional_failed += 1
    finally:
        if agent is not None:
            agent.terminate()
            try:
                agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()

    total = len(CHECKS)
    print("\n" + "-" * 64)
    print(f"通过 {passed}/{total}　必做失败 {required_failed}　加练失败 {optional_failed}")
    if required_failed:
        print("结论：还没到 7/7。建议顺序：C1（能发出请求）→ C3（纯函数，最好改）")
        print("      → C4（组装）→ C5/C6（两条错误路径，今天真正的考点）。")
    else:
        print("结论：全绿。你已经能用真协议读设备了 —— 这是 D8，也是真采集的起点。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
