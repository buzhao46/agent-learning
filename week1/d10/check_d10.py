"""D10 验收：WALK 接口表。

七项：
  C1  walk 读回 ifDescr 四行，值正确（含中文接口名）
  C2  walk 不越界：结果恰好 4 行，全部在 ifDescr 子树内
  C3  walk 行数上限：max_rows=3 就只给 3 行；设备原地打转时也不许挂死
  C4  if_index 取行号（含多段后缀的表）
  C5  build_interfaces 按行号合并：设备少回一行 status 时不错位
  C6  状态枚举映射 + 表格按显示宽度对齐
  C7  [加练] python ifwalk.py --port ... 端到端

验收会自己拉起四台 fake_agent.py：
  正常 / --miss-status / --many / --loop，跑完自动全部关掉。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "ifwalk.py"
AGENT = ROOT / "fake_agent.py"

VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"

# 一条真实的 SNMPv2c GET sysDescr 报文（真包），用来独立探活
PING_PACKET = bytes.fromhex(
    "302802010104067075626c6963a01b020344e2a8"
    "020100020100300e300c06082b060102010101000500"
)

BASE_DESCR = "1.3.6.1.2.1.2.2.1.2"
BASE_OPER = "1.3.6.1.2.1.2.2.1.8"
BASE_OCTETS = "1.3.6.1.2.1.2.2.1.10"

EXPECT_NAMES = ["GE0/0/1", "GE0/0/2", "上联口-核心", "XGE0/0/4"]


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


# ---------------------------------------------------------------- 子进程片段
PROBE = r"""
import json, sys
import ifwalk as w

def emit(payload):
    print("###" + json.dumps(payload, ensure_ascii=False))

action = sys.argv[1]
args = sys.argv[2:]

def three(port):
    d = w.walk("127.0.0.1", w.OID_IF_DESCR, port=port)
    s = w.walk("127.0.0.1", w.OID_IF_OPER, port=port)
    o = w.walk("127.0.0.1", w.OID_IF_IN_OCTETS, port=port)
    return d, s, o

try:
    if action == "walk":
        rows = w.walk("127.0.0.1", w.OID_IF_DESCR, port=int(args[0]))
        emit({"ok": True, "value": [[str(a), str(b)] for a, b in rows]})
    elif action == "walk_limit":
        rows = w.walk("127.0.0.1", w.OID_IF_DESCR, port=int(args[0]), max_rows=int(args[1]))
        emit({"ok": True, "value": [[str(a), str(b)] for a, b in rows]})
    elif action == "index":
        cases = [
            ("1.3.6.1.2.1.2.2.1.2.7", "1.3.6.1.2.1.2.2.1.2"),
            ("1.3.6.1.2.1.2.2.1.8.12", "1.3.6.1.2.1.2.2.1.8"),
            ("1.3.6.1.2.1.4.20.1.1.10.20.30.1", "1.3.6.1.2.1.4.20.1.1"),
        ]
        emit({"ok": True, "value": [w.if_index(a, b) for a, b in cases]})
    elif action == "interfaces":
        d, s, o = three(int(args[0]))
        rows = w.build_interfaces(d, s, o)
        emit({
            "ok": True,
            "type": type(rows).__name__,
            "value": [[r.index, r.name, r.status, r.in_octets] for r in rows],
        })
    elif action == "table":
        d, s, o = three(int(args[0]))
        emit({"ok": True, "value": w.fmt_table(w.build_interfaces(d, s, o))})
    else:
        emit({"ok": False, "type": "ValueError", "msg": "未知 action"})
except BaseException as exc:
    emit({"ok": False, "type": type(exc).__name__, "msg": str(exc)})
"""


def run(action: str, *args: str, timeout: int = 40) -> dict[str, object]:
    try:
        proc = subprocess.run(
            [interpreter(), "-c", PROBE, action, *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=make_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "type": "TimeoutExpired",
            "msg": f"{timeout} 秒还没结束 —— 多半是 WALK 没有行数上限，遇到不递增的 OID 就一直转",
        }
    for line in (proc.stdout or "").splitlines():
        if line.startswith("###"):
            return json.loads(line[3:])
    blob = (proc.stdout or "") + (proc.stderr or "")
    return {
        "ok": False,
        "type": "验收没拿到结果",
        "msg": "\n".join(blob.strip().splitlines()[-6:]) or "(空输出)",
    }


def describe(result: dict[str, object]) -> str:
    if result.get("ok"):
        return f"返回 {result.get('value')!r}"
    return f"抛了 {result.get('type')}: {result.get('msg')}"


# ---------------------------------------------------------------- 七项
def check_c1(ports: dict[str, int]) -> tuple[bool, str]:
    got = run("walk", str(ports["normal"]))
    if not got.get("ok"):
        return False, f"walk 对正常设备失败。{describe(got)}"
    rows = got["value"]
    if not isinstance(rows, list) or not rows:
        return False, f"walk 应返回 [(OID, 值), ...]，实际 {rows!r}"
    names = [row[1] for row in rows]
    for want in EXPECT_NAMES:
        if want not in names:
            return False, (
                f"结果里没有接口 {want!r}，实际拿到 {names}\n"
                "如果中文名变成了 0xe4b88ae8... 这种串，说明用了 prettyPrint()，"
                "OctetString 要改用 asOctets().decode('utf-8')。"
            )
    if names[:4] != EXPECT_NAMES:
        return False, f"前四个接口应是 {EXPECT_NAMES}，实际 {names[:4]}"
    return True, f"4 个接口名全部正确，中文没变成十六进制：{names[:4]}"


def check_c2(ports: dict[str, int]) -> tuple[bool, str]:
    got = run("walk", str(ports["normal"]))
    if not got.get("ok"):
        return False, f"walk 对正常设备失败。{describe(got)}"
    rows = got["value"]
    if len(rows) != 4:
        outside = [oid for oid, _ in rows if not oid.startswith(BASE_DESCR + ".")]
        sample = "、".join(outside[:3]) if outside else "（都在子树内，只是行数不对）"
        return False, (
            f"ifDescr 只有 4 行，实际返回 {len(rows)} 行。跑出子树的行例如：{sample}\n"
            "walkCmd 默认会一路走到整棵 MIB 的末尾，必须传 lexicographicMode=False，"
            "它才会在离开 base_oid 子树时停下。"
        )
    stray = [oid for oid, _ in rows if not oid.startswith(BASE_DESCR + ".")]
    if stray:
        return False, f"这些 OID 不在 ifDescr 子树内：{stray[:3]}"
    return True, "恰好 4 行，且全部落在 ifDescr 子树内（没有走到 ifType / ipAdEntAddr）"


def check_c3(ports: dict[str, int]) -> tuple[bool, str]:
    limited = run("walk_limit", str(ports["many"]), "3")
    if not limited.get("ok"):
        return False, f"对 50 接口的设备设 max_rows=3 时失败。{describe(limited)}"
    rows = limited["value"]
    if not isinstance(rows, list) or len(rows) != 3:
        return False, (
            f"max_rows=3 应只返回 3 行，实际返回 {len(rows) if isinstance(rows, list) else rows}\n"
            "在生成器的循环体里数行数，到 max_rows 就 break。"
        )

    looping = run("walk", str(ports["loop"]), timeout=20)
    if looping.get("type") == "TimeoutExpired":
        return False, (
            "对「表末尾原地打转」的设备，WALK 20 秒还没停下来。\n"
            "行数上限不只是省内存，它是唯一能保证 WALK 一定结束的东西。\n"
            "把 max_rows 计数放在循环里，每拿一行就检查一次。"
        )
    if not looping.get("ok"):
        # 抛异常也算「停下来了」，只要不是挂死就行
        return True, f"max_rows=3 生效；打转设备上抛 {looping.get('type')} 后结束（没挂死）"
    return True, f"max_rows=3 恰好 3 行；打转设备上返回 {len(looping['value'])} 行后停下（没挂死）"


def check_c4(ports: dict[str, int]) -> tuple[bool, str]:
    got = run("index")
    if not got.get("ok"):
        return False, f"if_index 失败。{describe(got)}"
    values = got["value"]
    expect = [7, 12, 10]
    if values != expect:
        return False, (
            f"if_index 应返回 {expect}，实际 {values}\n"
            "第三组是 IP 地址表：base 之后有四段（10.20.30.1），要取的是第一段 10。\n"
            "写死 split('.')[-1] 会得到 1 —— 只在单段行号的表上碰巧正确。"
        )
    return True, f"三组行号全部正确：{values}"


def check_c5(ports: dict[str, int]) -> tuple[bool, str]:
    got = run("interfaces", str(ports["miss"]))
    if not got.get("ok"):
        return False, f"build_interfaces 在缺 status 行的设备上失败。{describe(got)}"
    rows = got["value"]
    if not isinstance(rows, list) or len(rows) != 4:
        return False, f"应以 ifDescr 为准返回 4 行，实际 {len(rows) if isinstance(rows, list) else rows}"
    indexes = [r[0] for r in rows]
    if indexes != [1, 2, 3, 4]:
        return False, f"行号应是 [1,2,3,4]，实际 {indexes}"
    names = [r[1] for r in rows]
    if names != EXPECT_NAMES:
        return False, f"接口名应是 {EXPECT_NAMES}，实际 {names}"
    statuses = [r[2] for r in rows]
    if statuses != ["up", "up", "未知", "up"]:
        return False, (
            f"状态应是 ['up','up','未知','up']（设备就是没回第 3 个接口的 status），实际 {statuses}\n"
            "如果变成 ['up','up','up',...] 或整体错位，说明用 zip() 合并了 —— "
            "必须按行号查字典，缺的那行给「未知」，不能拿下一行的值顶上来。"
        )
    octets = [r[3] for r in rows]
    if octets != [1284993201, 88120442, 4294967100, 512004]:
        return False, (
            f"入字节数错位了：{octets}\n"
            "第 3 个接口应该是 4294967100。对不上就说明三列没有按行号对齐。"
        )
    return True, "缺一行 status 时不错位：第 3 个接口「未知」，其它三列数值全部归位"


def check_c6(ports: dict[str, int]) -> tuple[bool, str]:
    got = run("table", str(ports["normal"]))
    if not got.get("ok"):
        return False, f"fmt_table / build_interfaces 失败。{describe(got)}"
    text = str(got["value"])
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 6:
        return False, f"表格至少要有 表头 + 分隔线 + 4 行数据，实际只有 {len(lines)} 行：\n{text}"
    if "down" not in text:
        return False, (
            f"第 3 个接口是 down，但输出里没有 down。实际输出：\n{text}\n"
            "ifOperStatus 要过一张枚举表：1→up 2→down 3→testing 4→unknown "
            "5→dormant 6→notPresent 7→lowerLayerDown。直接打 1/2 不算完成。"
        )
    if "上联口-核心" not in text:
        return False, f"表格里没有「上联口-核心」：\n{text}"

    starts = [col_starts(ln) for ln in lines]
    if any(s != starts[0] for s in starts):
        detail = "\n".join(
            f"    列起点 {str(col_starts(ln)):<18} | {ln}" for ln in lines[:6]
        )
        return False, (
            "表格没有对齐：各行的列起始位置不一致（同一列应该都从同一格开始）。\n"
            + detail
            + "\n中文在终端里占两格，用 len() 算不出真实宽度。用 pad() 按 disp_width 补空格。"
        )
    return True, f"6 行齐全、down 正确、中文接口名在列，列起点统一在 {starts[0]}"


def check_c7(ports: dict[str, int]) -> tuple[bool, str]:
    proc = subprocess.run(
        [interpreter(), str(ENTRY), "--port", str(ports["normal"])],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=make_env(),
        timeout=60,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, f"退出码 {proc.returncode}\n{tail(out)}"
    if "上联口-核心" not in out or "down" not in out:
        return False, f"输出里没有接口表的关键内容：\n{tail(out)}"
    return True, "命令行走通，4 个接口 + down 状态都打出来了"


def col_starts(line: str) -> tuple[int, ...]:
    """每段非空白内容的起始列号（按显示宽度算，中文占 2 格）。

    用「列起点是否一致」判断对齐，而不是「整行宽度是否一致」——
    行尾空格被 rstrip 掉是正常的，不该因此判为没对齐。
    """
    pos = 0
    starts: list[int] = []
    prev_space = True
    for ch in line.rstrip():
        if ch != " " and prev_space:
            starts.append(pos)
        prev_space = ch == " "
        pos += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return tuple(starts)


def tail(text: str, n: int = 600) -> str:
    body = (text or "").strip()
    return "\n".join(body.splitlines()[-10:])[:n] if body else "(空)"


CHECKS = [
    ("C1", "walk 读回四行，中文接口名没坏", check_c1, True),
    ("C2", "walk 不越界（lexicographicMode=False）", check_c2, True),
    ("C3", "walk 有行数上限，打转设备不挂死", check_c3, True),
    ("C4", "if_index 取行号", check_c4, True),
    ("C5", "build_interfaces 按行号合并，缺行不错位", check_c5, True),
    ("C6", "状态枚举 + 表格按显示宽度对齐", check_c6, True),
    ("C7", "python ifwalk.py --port ...", check_c7, False),
]


def ready() -> tuple[bool, str]:
    missing = [n for n in ("ifwalk.py", "snmpio.py", "fake_agent.py") if not (ROOT / n).exists()]
    if missing:
        return False, f"缺文件：{', '.join(missing)}"
    src = ENTRY.read_text(encoding="utf-8")
    todo = len(re.findall(r"^\s*raise NotImplementedError", src, re.M))
    if todo:
        return False, f"ifwalk.py 里还有 {todo} 个 raise NotImplementedError —— TODO 没写完"
    try:
        subprocess.run(
            [interpreter(), "-c", "import pysnmp"],
            capture_output=True,
            env=make_env(),
            timeout=60,
            check=True,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"这个解释器里没有 pysnmp：{exc}"
    return True, f"ifwalk.py 的 TODO 已全部填完；pysnmp 可用（{interpreter()}）"


def main() -> int:
    print("=" * 64)
    print("D10 验收：WALK 接口表")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")

    ok_ready, info = ready()
    print(("准备：" + info) if ok_ready else ("准备：[!] " + info))

    took: set[int] = set()
    ports = {
        "normal": free_port(took),
        "miss": free_port(took),
        "many": free_port(took),
        "loop": free_port(took),
    }
    agents: list[subprocess.Popen[bytes]] = []
    all_alive = False
    if ok_ready:
        agents = [
            start_agent(ports["normal"]),
            start_agent(ports["miss"], "--miss-status"),
            start_agent(ports["many"], "--many"),
            start_agent(ports["loop"], "--loop"),
        ]
        alive = {name: wait_agent(port) for name, port in ports.items()}
        all_alive = all(alive.values())
        table = "　".join(
            f"{name} {ports[name]}{'' if alive[name] else ' [!]'}" for name in ports
        )
        print("假设备：" + table)

    passed = required_failed = optional_failed = 0
    try:
        for label, desc, fn, required in CHECKS:
            print(f"\n{label}  {desc}" + ("" if required else "　[加练]"))
            if not ok_ready or not all_alive:
                print("    [skip] 跳过（准备检查没过，或假设备没起来）")
                if required:
                    required_failed += 1
                else:
                    optional_failed += 1
                continue
            try:
                good, message = fn(ports)
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
        print("结论：还没全绿。顺序建议：C1（能读到）→ C2（不越界）→ C3（有上限）")
        print("      → C4（取行号）→ C5（按行号合并）→ C6（枚举 + 对齐）。")
    else:
        print("结论：全绿。你现在能从一台设备上读回一张行数未知的表，并拼成能看的报告了。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
