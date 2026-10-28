"""D9 验收：结果转 dataclass + safe_int。

七项：
  C1  safe_int 基本转换（字符串 / 带空格 / 已经是 int）
  C2  safe_int 挡住脏值（空串 / N/A / 小数 / 下划线 / bool / None / float）
  C3  safe_int 语义（负数默认拒绝可放行、哨兵值拒绝、临界值放行）
  C4  DeviceHealth：uptime_text、to_dict、frozen 不可改
  C5  build_health：脏值 → None 不抛；缺键不抛
  C6  probe_health 真采集：正常设备出 ticks，脏 uptime 设备降级为 None 且不抛
  C7  [加练] python health.py --port ... 能打印

验收会自己拉起两台 fake_agent.py（一台正常、一台 uptime 是脏值），跑完自动关掉。
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
ENTRY = ROOT / "health.py"
AGENT = ROOT / "fake_agent.py"

VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"

# 一条真实的 SNMPv2c GET sysDescr 报文（从 pysnmp 抓下来的真包），用来独立探活
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
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def snmp_ping(port: int, timeout: float = 0.4) -> bool:
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


# 在子进程里调学员的函数 —— 她写崩了只会让这一项红，不会把验收脚本一起带崩
PROBE = r"""
import json, sys
import health as h

action = sys.argv[1]
port = int(sys.argv[2])
bad_port = int(sys.argv[3])


def emit(payload):
    print("###" + json.dumps(payload, ensure_ascii=False))


def flat(obj):
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, (list, tuple)):
        return [flat(x) for x in obj]
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)


def is_probe(obj):
    try:
        r = h.probe_health("127.0.0.1", port=obj)
    except BaseException as exc:
        raise
    return {"type": type(r).__name__, **r.to_dict()}


try:
    if action == "safe_basic":
        out = [h.safe_int("12345678"), h.safe_int(" 42 "), h.safe_int(7)]
    elif action == "safe_dirty":
        out = [
            h.safe_int(""),
            h.safe_int("   "),
            h.safe_int("N/A"),
            h.safe_int("12.5"),
            h.safe_int("1_000"),
            h.safe_int(True),
            h.safe_int(None),
            h.safe_int(3.0),
        ]
    elif action == "safe_default":
        out = [h.safe_int("", default=0), h.safe_int("N/A", default=0)]
    elif action == "safe_sem":
        out = [
            h.safe_int(-1),
            h.safe_int(-5),
            h.safe_int(-5, allow_negative=True),
            h.safe_int(4294967295),
            h.safe_int(2147483647),
            h.safe_int(4294967294),
        ]
    elif action == "health_obj":
        a = h.DeviceHealth(host="10.0.0.1", sys_name="core-sw-01", sys_descr="Huawei", uptime_ticks=12345678)
        b = h.DeviceHealth(host="10.0.0.2", sys_name="agg-sw-03", sys_descr="Huawei", uptime_ticks=None)
        frozen = False
        try:
            b.uptime_ticks = 0
        except Exception:
            frozen = True
        out = {"a_text": a.uptime_text, "a_dict": a.to_dict(),
               "b_text": b.uptime_text, "b_dict": b.to_dict(), "frozen": frozen}
    elif action == "build_dirty":
        out = flat(h.build_health("10.0.0.9", {"sys_descr": "Huawei", "sys_name": "agg-sw-03", "sys_uptime": "N/A"}))
    elif action == "build_missing":
        out = flat(h.build_health("10.0.0.9", {"sys_name": "agg-sw-03"}))
    elif action == "probe_ok":
        out = is_probe(port)
    elif action == "probe_bad":
        out = is_probe(bad_port)
    else:
        raise SystemExit("未知 action: " + action)
    emit({"ok": True, "value": out})
except BaseException as exc:
    emit({"ok": False, "type": type(exc).__name__, "msg": str(exc)})
"""


def probe(action: str, port: int = 0, bad_port: int = 0, timeout: int = 40) -> dict[str, object]:
    proc = subprocess.run(
        [interpreter(), "-c", PROBE, action, str(port), str(bad_port)],
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
        "msg": "\n".join(blob.strip().splitlines()[-6:]) or "(空输出)",
    }


def tail(text: str, n: int = 500) -> str:
    body = (text or "").strip()
    return "\n".join(body.splitlines()[-8:])[:n] if body else "(空)"


def describe(result: dict[str, object]) -> str:
    if result.get("ok"):
        return f"返回 {result.get('value')!r}"
    return f"抛了 {result.get('type')}: {result.get('msg')}"


# ============================================================ 七项检查
def check_c1(port: int, bad_port: int) -> tuple[bool, str]:
    result = probe("safe_basic", port, bad_port)
    if not result.get("ok"):
        return False, f"safe_int 基本转换就崩了。{describe(result)}"
    got = result["value"]
    want = [12345678, 42, 7]
    if got != want:
        return False, (
            f"safe_int('12345678') / safe_int(' 42 ') / safe_int(7) 应得 {want}，实际 {got}\n"
            "第二个不对多半是没 strip()：先 strip 再匹配正则，别在正则里留空格。"
        )
    return True, f"字符串 / 带空格 / int 都能转：{got}"


def check_c2(port: int, bad_port: int) -> tuple[bool, str]:
    result = probe("safe_dirty", port, bad_port)
    if not result.get("ok"):
        return False, f"safe_int 遇到脏值就崩了（应该返回 None，不是抛异常）。{describe(result)}"
    got = result["value"]
    labels = ["''", "'   '", "'N/A'", "'12.5'", "'1_000'", "True", "None", "3.0"]
    bad = {labels[i]: got[i] for i in range(len(labels)) if got[i] is not None}
    if bad:
        hints = {
            "'1_000'": "用了 try: int(text) —— Python 认下划线，'1_000' 会变成 1000。改用正则白名单。",
            "'12.5'": "先转 float 再转 int 了 —— 静默截断比报错更危险。小数一律拒绝。",
            "True": "没先挡 bool：isinstance(True, int) 是 True，它混在 int 里必须先拦。",
            "3.0": "float 不该接受，SNMP 不返回浮点；接受它等于接受了上游的类型错误。",
        }
        detail = "\n".join(f"    {k} → {v}　{hints.get(k, '')}" for k, v in bad.items())
        return False, f"这些脏值没挡住（应全部为 None）：\n{detail}"
    return True, "八种脏值全部挡住，返回 None 而不是抛异常"


def check_c3(port: int, bad_port: int) -> tuple[bool, str]:
    result = probe("safe_sem", port, bad_port)
    if not result.get("ok"):
        return False, f"safe_int 语义检查崩了。{describe(result)}"
    got = result["value"]
    want = [None, None, -5, None, None, 4294967294]
    if got != want:
        notes = []
        if got[0] is not None:
            notes.append("-1 没拦：SNMP 计数器是无符号的，负数几乎都是「不支持」的哨兵")
        if got[1] is not None:
            notes.append("-5 也该默认拦掉，除非调用方显式开 allow_negative")
        if got[2] != -5:
            notes.append("allow_negative=True 没生效 —— 这个开关没接进判断")
        if got[3] is not None or got[4] is not None:
            notes.append("哨兵值 4294967295 / 2147483647 没判 —— 设备固件用它们表示「我没数据」")
        if got[5] != 4294967294:
            notes.append(
                "4294967294 被误杀了 —— 说明你是按「数字太大」砍的，不是按哨兵值判的。\n"
                "      sysUpTime 跑几年的设备 ticks 能到几十亿，砍掉的是真数据。"
            )
        return False, f"应得 {want}，实际 {got}\n  " + "\n  ".join(notes)
    return True, "负数默认拒绝可放行；哨兵拒绝；4294967294 正常放行"


def check_c4(port: int, bad_port: int) -> tuple[bool, str]:
    result = probe("health_obj", port, bad_port)
    if not result.get("ok"):
        return False, f"构造 DeviceHealth 就崩了。{describe(result)}"
    got = result["value"]
    if got.get("a_text") != "1 day, 10:17:36.78":
        return False, (
            f"有 ticks 时 uptime_text 应为 '1 day, 10:17:36.78'，实际 {got.get('a_text')!r}\n"
            "应该复用 snmpio.fmt_ticks，别自己再算一遍。"
        )
    if got.get("b_text") != "未知":
        return False, (
            f"uptime_ticks 为 None 时 uptime_text 应为 '未知'，实际 {got.get('b_text')!r}\n"
            "'00:00:00.00' 是「刚启动」，不是「不知道」—— 用真值表示缺失等于撒谎。"
        )
    for key in ("host", "sys_name", "sys_descr", "uptime_ticks", "uptime_text"):
        if key not in got["a_dict"]:
            return False, f"to_dict() 缺键 {key!r}，实际有 {list(got['a_dict'])}"
    if not got.get("frozen"):
        return False, (
            "DeviceHealth 居然能改（uptime_ticks = 0 没报错）。\n"
            "采集结果是快照，要 @dataclass(frozen=True)；要改请用 dataclasses.replace()。"
        )
    return True, "uptime_text 两种分支都对，to_dict 五个键齐全，frozen 生效"


def check_c5(port: int, bad_port: int) -> tuple[bool, str]:
    result = probe("build_dirty", port, bad_port)
    if not result.get("ok"):
        if result.get("type") == "KeyError":
            return False, f"build_health 对缺键抛了 KeyError：{result.get('msg')}\n用 raw.get(k, '')。"
        return False, f"build_health 处理脏值崩了。{describe(result)}"
    got = result["value"]
    if got.get("uptime_ticks") is not None:
        return False, (
            f"sys_uptime='N/A' 时 uptime_ticks 应为 None，实际 {got.get('uptime_ticks')!r}\n"
            "0 和 None 是两回事：0 表示刚重启，是有意义的真值。"
        )
    if got.get("uptime_text") != "未知":
        return False, f"脏值时 uptime_text 应为 '未知'，实际 {got.get('uptime_text')!r}"

    result2 = probe("build_missing", port, bad_port)
    if not result2.get("ok"):
        return False, (
            f"raw 里缺键时崩了：{describe(result2)}\n"
            "缺键本身也是一种脏输入，不该让整轮巡检挂掉 —— 用 raw.get(k, '')。"
        )
    return True, "脏 uptime → None 且显示「未知」；缺键不抛 KeyError"


def check_c6(port: int, bad_port: int) -> tuple[bool, str]:
    result = probe("probe_ok", port, bad_port)
    if not result.get("ok"):
        return False, f"probe_health 对正常设备失败。{describe(result)}"
    got = result["value"]
    if got.get("type") != "DeviceHealth":
        return False, f"probe_health 应返回 DeviceHealth，实际返回 {got.get('type')}"
    if got.get("sys_name") != "core-sw-01":
        return False, f"sys_name 应为 'core-sw-01'，实际 {got.get('sys_name')!r}"
    if got.get("uptime_ticks") != 12345678:
        return False, f"uptime_ticks 应为 12345678，实际 {got.get('uptime_ticks')!r}"
    if got.get("uptime_text") != "1 day, 10:17:36.78":
        return False, f"uptime_text 应为 '1 day, 10:17:36.78'，实际 {got.get('uptime_text')!r}"

    bad = probe("probe_bad", port, bad_port)
    if not bad.get("ok"):
        return False, (
            f"设备回脏 uptime（'N/A'）时抛了异常：{describe(bad)}\n"
            "这是今天的设计决策：uptime 一个字段不可信，不该让整台设备判失败。\n"
            "只给 uptime 那条 snmp_get 兜一层 try/except ProbeError。"
        )
    bgot = bad["value"]
    if bgot.get("uptime_ticks") is not None:
        return False, f"脏 uptime 应降级为 None，实际 {bgot.get('uptime_ticks')!r}"
    if bgot.get("sys_name") != "core-sw-01":
        return False, (
            f"降级收得太宽了：sys_name 也没了（{bgot.get('sys_name')!r}）。\n"
            "只兜 uptime，别写 `try: 三个都采 except: pass`。"
        )
    return True, "正常设备出 ticks；脏 uptime 降级为 None，sys_name 仍保留"


def check_c7(port: int, bad_port: int) -> tuple[bool, str]:
    proc = subprocess.run(
        [interpreter(), "health.py", "--host", "127.0.0.1", "--port", str(port)],
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
    missing = [s for s in ("Huawei", "core-sw-01", "1 day, 10:17:36.78") if s not in out]
    if missing:
        return False, f"输出里看不到 {missing}\n实际输出：\n{tail(out)}"
    return True, "命令行走通，型号 / 名称 / 运行都打出来了"


CHECKS: list[tuple[str, str, object, bool]] = [
    ("C1", "safe_int 基本转换", check_c1, True),
    ("C2", "safe_int 挡住八种脏值", check_c2, True),
    ("C3", "负数 / 哨兵 / 临界值", check_c3, True),
    ("C4", "DeviceHealth：文本、to_dict、frozen", check_c4, True),
    ("C5", "build_health 脏值与缺键", check_c5, True),
    ("C6", "probe_health 真采集 + 字段级降级", check_c6, True),
    ("C7", "python health.py --port ...", check_c7, False),
]


def ready() -> tuple[bool, str]:
    missing = [n for n in ("health.py", "snmpio.py", "fake_agent.py") if not (ROOT / n).exists()]
    if missing:
        return False, f"缺文件：{', '.join(missing)}"
    src = ENTRY.read_text(encoding="utf-8")
    if re.search(r"^\s*raise NotImplementedError", src, re.M):
        return False, "health.py 里还有 raise NotImplementedError —— 四个 TODO 还没写完"
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
    return True, "health.py 已无占位，pysnmp 可导入"


def start_agent(port: int, *, bad: bool = False) -> subprocess.Popen[bytes]:
    cmd = [interpreter(), "fake_agent.py", "--port", str(port)]
    if bad:
        cmd.append("--uptime-bad")
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=make_env(),
    )


def main() -> int:
    print("=" * 64)
    print("D9 验收：结果转 dataclass + safe_int")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")

    ok_ready, info = ready()
    print(("准备：" + info) if ok_ready else ("准备：[!] " + info))

    port = free_port()
    bad_port = free_port()
    agents: list[subprocess.Popen[bytes]] = []
    alive = bad_alive = False
    if ok_ready:
        agents.append(start_agent(port))
        agents.append(start_agent(bad_port, bad=True))
        alive = wait_agent(port)
        bad_alive = wait_agent(bad_port)
        print(
            f"假设备：正常 127.0.0.1:{port}" + ("" if alive else "  [!] 没起来")
            + f"　脏 uptime 127.0.0.1:{bad_port}" + ("" if bad_alive else "  [!] 没起来")
        )

    passed = required_failed = optional_failed = 0
    try:
        for label, desc, fn, required in CHECKS:
            print(f"\n{label}  {desc}" + ("" if required else "　[加练]"))
            if not ok_ready:
                print("    [skip] 跳过（准备检查没过）")
                if required:
                    required_failed += 1
                else:
                    optional_failed += 1
                continue
            if not (alive and bad_alive):
                print("    [skip] 跳过（假设备没起来，可能是端口被占或 fake_agent.py 被改过）")
                if required:
                    required_failed += 1
                else:
                    optional_failed += 1
                continue
            try:
                good, message = fn(port, bad_port)  # type: ignore[operator]
            except Exception as exc:  # noqa: BLE001 - 验收脚本自己绝不能崩
                good, message = False, f"验收时抛异常：{type(exc).__name__}: {exc}"
            print(f"    [{'OK' if good else 'NG'}] {message}")
            if good:
                passed += 1
            elif required:
                required_failed += 1
            else:
                optional_failed += 1
    finally:
        for agent in agents:
            agent.terminate()
            try:
                agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()

    total = len(CHECKS)
    print("\n" + "-" * 64)
    print(f"通过 {passed}/{total}　必做失败 {required_failed}　加练失败 {optional_failed}")
    if required_failed:
        print("结论：还没全绿。建议顺序：C1（能转）→ C2（挡脏值，今天的核心）→ C3（语义）")
        print("      → C4（dataclass）→ C5（纯函数层）→ C6（真采集 + 字段级降级）。")
    else:
        print("结论：全绿。采集结果现在是「类型明确、脏值有解」的数据了 —— D10 可以直接往上堆 WALK。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
