"""D11 验收：落盘 CSV + JSON。

七项：
  C1  to_rows 摊平：字段齐全、None 原样保留（不是 "-" / 0）
  C2  CSV 字节：有 BOM、无空行、中文正常
  C3  JSON：能 loads（说明没 BOM）、中文非转义、None → null、有 schema
  C4  原子写：写完后目录里没有 .tmp 残留
  C5  文件名不覆盖：CLI 连跑两次是 4 个文件（2 csv + 2 json）
  C6  端到端保真：缺 status 行 → 「未知」但字节数还在；脏 uptime → null
  C7  [加练] python save.py --port ... 退出码 0 且打出两个路径

验收会自己拉起三台 fake_agent.py：正常 / --miss-status / --uptime-bad，跑完自动关掉。
C1–C4 不需要设备（纯函数和写文件），C5–C7 需要。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "save.py"
AGENT = ROOT / "fake_agent.py"

VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"

# 一条真实的 SNMPv2c GET sysDescr 报文（真包），用来独立探活
PING_PACKET = bytes.fromhex(
    "302802010104067075626c6963a01b020344e2a8"
    "020100020100300e300c06082b060102010101000500"
)

BOM = b"\xef\xbb\xbf"
CSV_FIELDS = ("host", "collected_at", "index", "name", "status", "in_octets")


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
import json, sys, tempfile, pathlib
import save as s
import ifwalk as w
import health as h

def emit(payload):
    print("###" + json.dumps(payload, ensure_ascii=False))

action = sys.argv[1]
args = sys.argv[2:]

SAMPLE = [
    w.Interface(1, "GE0/0/1", "up", 100),
    w.Interface(2, "上联口-核心", "未知", None),
]

try:
    if action == "rows":
        emit({"ok": True, "value": s.to_rows("core-sw-01", SAMPLE, "2026-10-28T10:49:35")})
    elif action == "files":
        d = pathlib.Path(tempfile.mkdtemp(prefix="d11_"))
        device = h.DeviceHealth(host="127.0.0.1", sys_name="core-sw-01", sys_descr="fake")
        payload = s.build_payload(
            "core-sw-01", device, SAMPLE,
            collected_at="2026-10-28T10:49:35", collected_at_ts=0.0,
        )
        p_csv = s.write_csv(d / "t.csv", s.to_rows("core-sw-01", SAMPLE, "2026-10-28T10:49:35"))
        p_json = s.write_json(d / "t.json", payload)
        emit({"ok": True, "value": {"dir": str(d), "csv": str(p_csv), "json": str(p_json)}})
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
            "msg": f"{timeout} 秒还没结束",
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


def run_cli(port: int, outdir: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [interpreter(), str(ENTRY), "--port", str(port), "--out", outdir],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=make_env(),
        timeout=timeout,
    )


def describe(result: dict[str, object]) -> str:
    if result.get("ok"):
        return f"返回 {result.get('value')!r}"
    return f"抛了 {result.get('type')}: {result.get('msg')}"


def latest(directory: Path, pattern: str) -> Path:
    files = sorted(directory.glob(pattern))
    if not files:
        raise FileNotFoundError(f"{directory} 里没有 {pattern}")
    return files[-1]


# ---------------------------------------------------------------- 七项
def check_c1() -> tuple[bool, str]:
    got = run("rows")
    if not got.get("ok"):
        return False, f"to_rows 失败。{describe(got)}"
    rows = got["value"]
    if not isinstance(rows, list) or len(rows) != 2:
        return False, f"to_rows 应返回 2 行，实际 {rows!r}"
    keys = [tuple(row.keys()) for row in rows]
    if any(key != CSV_FIELDS for key in keys):
        return False, f"列的键应是 {CSV_FIELDS}，实际 {keys[0]}"
    if rows[0]["host"] != "core-sw-01" or rows[0]["collected_at"] != "2026-10-28T10:49:35":
        return False, f"host / collected_at 没写进每一行：{rows[0]!r}"
    if rows[1]["in_octets"] is not None:
        return False, (
            f"第 2 行 in_octets 是 None，落盘必须原样保留，实际 {rows[1]['in_octets']!r}\n"
            "写成 \"-\" 或 0 之后，「一个包都没收」和「没采到」就再也分不清了。"
        )
    return True, "6 列齐全、行内带 host/采集时间、None 原样保留"


def check_c2() -> tuple[bool, str]:
    got = run("files")
    if not got.get("ok"):
        return False, f"write_csv 失败。{describe(got)}"
    csv_path = Path(str(got["value"]["csv"]))
    if not csv_path.exists():
        return False, f"{csv_path} 不存在 —— write_csv 返回的路径不对"
    raw = csv_path.read_bytes()
    if not raw.startswith(BOM):
        return (
            False,
            f"CSV 开头没有 BOM（前 3 字节是 {raw[:3]!r}）。\n"
            "open(...) 要传 encoding=\"utf-8-sig\"，否则 Excel 打开中文全是乱码。",
        )
    if b"\r\r\n" in raw:
        return (
            False,
            "CSV 里有 \\r\\r\\n —— 每行之间会多一个空行。\n"
            "open(...) 要传 newline=\"\"，把换行完全交给 csv 模块。",
        )
    text = raw.decode("utf-8-sig")
    if "上联口-核心" not in text:
        return False, f"CSV 里没有「上联口-核心」（中文被转成了转义或乱码）：\n{text[:200]}"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) != 3:
        return False, f"CSV 应是「表头 + 2 行」，实际 {len(lines)} 行：\n{text[:200]}"
    return True, "有 BOM、无空行、中文正常、表头 + 2 行"


def check_c3() -> tuple[bool, str]:
    got = run("files")
    if not got.get("ok"):
        return False, f"write_json 失败。{describe(got)}"
    json_path = Path(str(got["value"]["json"]))
    if not json_path.exists():
        return False, f"{json_path} 不存在 —— write_json 返回的路径不对"
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        return False, (
            f"JSON 读不出来：{exc}\n"
            "多半是写了 BOM（用了 utf-8-sig）。JSON 要用 utf-8，"
            "否则 json.load 抛 Unexpected UTF-8 BOM。"
        )
    except json.JSONDecodeError as exc:
        return False, f"JSON 解析失败：{exc}\n文件内容前 200 字符：\n{json_path.read_text(encoding='utf-8', errors='replace')[:200]}"
    if "schema" not in payload:
        return False, "payload 里没有 schema 版本号 —— 三个月后你读不懂这份文件"
    interfaces = payload.get("interfaces", [])
    if len(interfaces) != 2:
        return False, f"interfaces 应有 2 个，实际 {len(interfaces)}"
    if interfaces[1]["name"] != "上联口-核心":
        return (
            False,
            f"中文没保住：{interfaces[1]['name']!r}\n"
            "json.dump 要传 ensure_ascii=False，否则中文变成 \\u4e0a\\u8054\\u53e3。",
        )
    if interfaces[1]["in_octets"] is not None:
        return False, f"in_octets 是 None，JSON 里应是 null，实际 {interfaces[1]['in_octets']!r}"
    return True, "能 loads（无 BOM）、中文非转义、None→null、带 schema"


def check_c4() -> tuple[bool, str]:
    got = run("files")
    if not got.get("ok"):
        return False, f"写文件失败。{describe(got)}"
    directory = Path(str(got["value"]["dir"]))
    leftover = sorted(p.name for p in directory.glob("*.tmp"))
    if leftover:
        return (
            False,
            f"目录里还剩临时文件：{leftover}\n"
            "write_json 应该先写 .tmp，再用 os.replace(tmp, path) 换成目标文件。",
        )
    files = sorted(p.name for p in directory.iterdir())
    if "t.csv" not in files or "t.json" not in files:
        return False, f"目录里没有 t.csv / t.json，实际 {files}"
    return True, f"临时文件被 replace 掉了，目录里只剩 {files}"


def check_c5(ports: dict[str, int]) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="d11_cli_") as tmp:
        outdir = Path(tmp)
        first = run_cli(ports["normal"], str(outdir))
        if first.returncode != 0:
            return False, f"第一次跑 save.py 退出码 {first.returncode}\n{tail(first.stdout + first.stderr)}"
        # 时间戳精确到秒，必须隔开一秒才看得出「没覆盖」
        time.sleep(1.2)
        second = run_cli(ports["normal"], str(outdir))
        if second.returncode != 0:
            return False, f"第二次跑 save.py 退出码 {second.returncode}\n{tail(second.stdout + second.stderr)}"
        csvs = sorted(outdir.glob("*.csv"))
        jsons = sorted(outdir.glob("*.json"))
        if len(csvs) != 2 or len(jsons) != 2:
            return (
                False,
                f"连跑两次应留下 2 份 csv + 2 份 json，实际 {len(csvs)} + {len(jsons)}"
                f"（{sorted(p.name for p in outdir.iterdir())}）\n"
                "文件名写死、或者时间戳只到分钟，第二次就把第一次冲掉了 —— "
                "巡检是时间序列，历史文件比代码值钱。",
            )
        if csvs[0].name == csvs[1].name:
            return False, f"两份文件名完全一样：{csvs[0].name}"
    return True, "连跑两次留下 4 个文件，没有互相覆盖"


def check_c6(ports: dict[str, int]) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="d11_miss_") as tmp:
        outdir = Path(tmp)
        proc = run_cli(ports["miss"], str(outdir))
        if proc.returncode != 0:
            return False, f"对 --miss-status 设备跑 save.py 失败\n{tail(proc.stdout + proc.stderr)}"
        try:
            payload = json.loads(latest(outdir, "*.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"读 JSON 失败：{exc}"
        third = payload["interfaces"][2]
        if third["status"] != "未知":
            return (
                False,
                f"第 3 个接口状态应是「未知」，实际 {third['status']!r}\n"
                "设备就是没回这一行的 ifOperStatus，别拿下一行的值顶上来。",
            )
        if third["in_octets"] != 4294967100:
            return (
                False,
                f"第 3 个接口入字节应是 4294967100，实际 {third['in_octets']!r}\n"
                "对不上说明三列没按行号对齐（D10 的 build_interfaces 出问题了）。",
            )

    with tempfile.TemporaryDirectory(prefix="d11_bad_") as tmp:
        outdir = Path(tmp)
        proc = run_cli(ports["bad"], str(outdir))
        if proc.returncode != 0:
            return False, f"对 --uptime-bad 设备跑 save.py 失败\n{tail(proc.stdout + proc.stderr)}"
        try:
            payload = json.loads(latest(outdir, "*.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"读 JSON 失败：{exc}"
        device = payload.get("device", {})
        if device.get("uptime_ticks") is not None:
            return (
                False,
                f"脏 uptime 落到磁盘上应是 null，实际 {device.get('uptime_ticks')!r}\n"
                "设备回的是 N/A（或 2147483647 / 4294967295 这类哨兵值）。\n"
                "safe_int（D9）→ DeviceHealth（D9）→ JSON（今天）这条链上，"
                "任何一环把 None 变成 0，后面就再也修不回来。",
            )
    return True, "缺状态行 → 「未知」但字节数还在；脏 uptime → null（不是 0 / -1）"


def check_c7(ports: dict[str, int]) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="d11_out_") as tmp:
        proc = run_cli(ports["normal"], tmp)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, f"退出码 {proc.returncode}\n{tail(out)}"
    if ".csv" not in out or ".json" not in out:
        return False, f"输出里没打出两个文件的路径：\n{tail(out)}"
    return True, "退出码 0，两个路径都打出来了"


def tail(text: str, n: int = 600) -> str:
    body = (text or "").strip()
    return "\n".join(body.splitlines()[-10:])[:n] if body else "(空)"


CHECKS = [
    ("C1", "to_rows 摊平，None 保真", check_c1, True, False),
    ("C2", "CSV：BOM + 无空行 + 中文", check_c2, True, False),
    ("C3", "JSON：无 BOM + 非转义 + null", check_c3, True, False),
    ("C4", "原子写，无 .tmp 残留", check_c4, True, False),
    ("C5", "文件名带时间戳，不覆盖历史", check_c5, True, True),
    ("C6", "端到端保真（缺行 / 脏 uptime）", check_c6, True, True),
    ("C7", "python save.py --port ...", check_c7, False, True),
]


def ready() -> tuple[bool, str]:
    missing = [
        n
        for n in ("save.py", "ifwalk.py", "health.py", "snmpio.py", "fake_agent.py")
        if not (ROOT / n).exists()
    ]
    if missing:
        return False, f"缺文件：{', '.join(missing)}"
    src = ENTRY.read_text(encoding="utf-8")
    todo = len(re.findall(r"^\s*raise NotImplementedError", src, re.M))
    if todo:
        return False, f"save.py 里还有 {todo} 个 raise NotImplementedError —— TODO 没写完"
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
    return True, f"save.py 的 TODO 已全部填完；pysnmp 可用（{interpreter()}）"


def main() -> int:
    print("=" * 64)
    print("D11 验收：落盘 CSV + JSON")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")

    ok_ready, info = ready()
    print(("准备：" + info) if ok_ready else ("准备：[!] " + info))

    took: set[int] = set()
    ports = {
        "normal": free_port(took),
        "miss": free_port(took),
        "bad": free_port(took),
    }
    agents: list[subprocess.Popen[bytes]] = []
    all_alive = False
    if ok_ready:
        agents = [
            start_agent(ports["normal"]),
            start_agent(ports["miss"], "--miss-status"),
            start_agent(ports["bad"], "--uptime-bad"),
        ]
        alive = {name: wait_agent(port) for name, port in ports.items()}
        all_alive = all(alive.values())
        table = "　".join(
            f"{name} {ports[name]}{'' if alive[name] else ' [!]'}" for name in ports
        )
        print("假设备：" + table)

    passed = required_failed = optional_failed = 0
    try:
        for label, desc, fn, required, need_agent in CHECKS:
            print(f"\n{label}  {desc}" + ("" if required else "　[加练]"))
            if not ok_ready or (need_agent and not all_alive):
                print("    [skip] 跳过（TODO 没写完，或假设备没起来）")
                if required:
                    required_failed += 1
                else:
                    optional_failed += 1
                continue
            try:
                good, message = fn(ports) if need_agent else fn()
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
        print("结论：还没全绿。顺序建议：C1（摊平）→ C2（CSV 三个参数）→ C3（JSON 两个参数）")
        print("      → C4（原子写）→ C5（文件名）→ C6（端到端，脏值必须还是 null）。")
    else:
        print("结论：全绿。采集结果现在是能交出去的文件了，D12 可以在它上面加错误分类。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
