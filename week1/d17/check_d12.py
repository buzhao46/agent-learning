"""D12 验收：错误分类 + 部分成功。

七项：
  C1  classify 纯函数：7 组输入 → 7 个类别（含「三个字段都正常」→ None）
  C2  get_raw 成功路径：值非空、没有 Failure
  C3  get_raw 失败路径：**不抛异常**、返回 TIMEOUT、且没重试（retries=0 生效）
  C4  community 错：4 项全 TIMEOUT（**不是** auth）、verdict=FAILED、退出码 1
  C5  部分成功：--gen-err + --no-such-name → 分类正确，另两项照常采到
  C6  --too-big：ifTable 单列失败，前 3 项完好，verdict=DEGRADED、退出码 2
  C7  [加练] CLI 端到端：正常设备退出码 0，4 项都打出来

验收会自己拉起三台 fake_agent.py：正常 / 两个错误码 / tooBig，跑完自动关掉。
C1 不需要设备（纯函数），C2–C7 需要。

C4 是今天的重点：SNMPv2c 里 community 错**就是**超时，没有「认证失败」这个
类别。如果 C4 报出来一个 auth 之类的 kind，说明分类里编了一个协议不存在的东西。
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
ENTRY = ROOT / "probe.py"
AGENT = ROOT / "fake_agent.py"

VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"

# 一条真实的 SNMPv2c GET sysDescr 报文（真包），用来独立探活
PING_PACKET = bytes.fromhex(
    "302802010104067075626c6963a01b020344e2a8020100020100300e300c06082b060102010101000500"
)

STAGES = ("sysDescr", "uptime", "sysName", "ifTable")


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
import json, sys, time
from pysnmp.proto import errind, rfc1905, rfc1902
import probe as p

def emit(payload):
    print("###" + json.dumps(payload, ensure_ascii=False, default=str))

def kindname(k):
    return None if k is None else getattr(k, "value", str(k))

action = sys.argv[1]
args = sys.argv[2:]

try:
    if action == "classify":
        cases = []

        def add(desc, ind, st, val, want):
            got = p.classify(ind, st, val)
            cases.append({"case": desc, "got": kindname(got), "want": want})

        add("设备无应答", errind.RequestTimedOut(), None, None, "timeout")
        add("空响应包", errind.EmptyResponse(), None, None, "protocol")
        add("端口不可达", errind.OtherError("destinationUnreachable"), None, None, "unreachable")
        add("tooBig(1)", None, rfc1905.errorStatus.clone(1), None, "too_big")
        add("noSuchName(2)", None, rfc1905.errorStatus.clone(2), None, "bad_oid")
        add("genErr(5)", None, rfc1905.errorStatus.clone(5), None, "device_error")
        add("NoSuchInstance", None, rfc1905.errorStatus.clone(0), rfc1905.NoSuchInstance(), "no_data")
        add("一切正常", None, rfc1905.errorStatus.clone(0), rfc1902.OctetString("ok"), None)
        emit({"ok": True, "value": cases})

    elif action == "get":
        host, port, community, timeout = args[0], int(args[1]), args[2], float(args[3])
        t0 = time.perf_counter()
        value, failure = p.get_raw(
            host, p.OID_SYSDESCR, community=community, port=port, timeout=timeout
        )
        elapsed = time.perf_counter() - t0
        emit({"ok": True, "value": {
            "value": value,
            "kind": kindname(getattr(failure, "kind", None)),
            "detail": getattr(failure, "detail", None),
            "stage": getattr(failure, "stage", None),
            "elapsed": round(elapsed, 3),
        }})

    elif action == "collect":
        host, port, community, timeout = args[0], int(args[1]), args[2], float(args[3])
        result = p.collect_device(host, community=community, port=port, timeout=timeout)
        emit({"ok": True, "value": {
            "values": {k: str(v) for k, v in result.values.items()},
            "failures": [
                {"stage": f.stage, "kind": kindname(f.kind), "detail": str(f.detail)}
                for f in result.failures
            ],
            "verdict": result.verdict,
            "exit_code": result.exit_code,
            "total": result.total,
        }})

    else:
        emit({"ok": False, "type": "ValueError", "msg": "未知 action"})
except BaseException as exc:
    emit({"ok": False, "type": type(exc).__name__, "msg": str(exc)})
"""


def run(action: str, *args: str, timeout: int = 60) -> dict[str, object]:
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
        return {"ok": False, "type": "TimeoutExpired", "msg": f"{timeout} 秒还没结束"}
    for line in (proc.stdout or "").splitlines():
        if line.startswith("###"):
            return json.loads(line[3:])
    blob = (proc.stdout or "") + (proc.stderr or "")
    return {
        "ok": False,
        "type": "验收没拿到结果",
        "msg": "\n".join(blob.strip().splitlines()[-6:]) or "(空输出)",
    }


def run_cli(port: int, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [interpreter(), str(ENTRY), "--port", str(port)],
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


def tail(text: str, n: int = 600) -> str:
    body = (text or "").strip()
    return "\n".join(body.splitlines()[-10:])[:n] if body else "(空)"


# ---------------------------------------------------------------- 七项
def check_c1() -> tuple[bool, str]:
    got = run("classify")
    if not got.get("ok"):
        return False, f"classify 没跑通。{describe(got)}"
    cases = got["value"]
    if not isinstance(cases, list) or len(cases) != 8:
        return False, f"应有 8 组用例，实际 {cases!r}"
    wrong = [c for c in cases if c["got"] != c["want"]]
    if wrong:
        detail = "\n".join(
            f"      {c['case']:<16} 期望 {c['want']!s:<14} 实际 {c['got']!s}" for c in wrong
        )
        return (
            False,
            f"{len(wrong)}/{len(cases)} 组分类不对：\n{detail}\n"
            "按判据顺序走：indication → status → value → None。\n"
            "「一切正常」那组要返回 None（不是某个 kind）。",
        )
    return True, "8 组输入全部分类正确（含「都正常」→ None）"


def check_c2(ports: dict[str, int]) -> tuple[bool, str]:
    got = run("get", "127.0.0.1", str(ports["normal"]), "public", "0.5")
    if not got.get("ok"):
        return False, f"get_raw 没跑通。{describe(got)}"
    value = got["value"]
    if value["kind"] is not None:
        return (
            False,
            f"正常设备却报了 {value['kind']}（{value['detail']}）\n"
            "classify 在「三个字段都正常」时要返回 None，不能返回任何 kind。",
        )
    if not value["value"] or "S5735" not in str(value["value"]):
        return False, f"值不对：{value['value']!r}（期望拿到 sysDescr 的型号串）"
    return True, f"拿到 sysDescr：{str(value['value'])[:48]}…"


def check_c3(ports: dict[str, int]) -> tuple[bool, str]:
    got = run("get", "127.0.0.1", str(ports["normal"]), "wrong-community", "0.4")
    if not got.get("ok"):
        return (
            False,
            f"get_raw 抛异常了，它应该**永不抛异常**。{describe(got)}\n"
            "用 (值, Failure) 二元组返回，别 raise。",
        )
    value = got["value"]
    if value["value"] is not None:
        return False, f"community 错了却拿到了值：{value['value']!r}"
    if value["kind"] != "timeout":
        return (
            False,
            f"community 错时 kind 应是 timeout，实际 {value['kind']!r}\n"
            "SNMPv2c 认证失败 = 设备静默丢包 = 客户端只看到超时。"
            "分类里不该有 auth 这一类。",
        )
    if not value["detail"]:
        return (
            False,
            "Failure.detail 是空的 —— 报告里只有「timeout」四个字母，看不出是哪台设备哪个 OID",
        )
    if float(value["elapsed"]) > 2.2:
        return (
            False,
            f"一次超时花了 {value['elapsed']}s（timeout 给的 0.4s）。\n"
            "retries=0 时约 1.1s（pysnmp 每次 getCmd 有 ~0.7s 固定开销）；\n"
            "不传的话默认重试 5 次，实测 3.6s —— UdpTransportTarget 要显式传 retries=0。",
        )
    return True, f"{value['elapsed']}s 内返回 TIMEOUT，带 detail：{str(value['detail'])[:40]}…"


def check_c4(ports: dict[str, int]) -> tuple[bool, str]:
    got = run("collect", "127.0.0.1", str(ports["normal"]), "wrong-community", "0.4", timeout=90)
    if not got.get("ok"):
        return False, f"collect_device 没跑通。{describe(got)}"
    value = got["value"]
    failures = value["failures"]
    if value["values"]:
        return False, f"community 错了却有 {list(value['values'])} 采到值"
    if len(failures) != len(STAGES):
        return (
            False,
            f"应有 {len(STAGES)} 条失败，实际 {len(failures)} 条：{[f['stage'] for f in failures]}\n"
            "不变量是 len(values) + len(failures) == 4 —— 失败的项不能从结果里消失。",
        )
    kinds = {f["kind"] for f in failures}
    illegal = kinds - {"timeout", "unreachable", "protocol"}
    if illegal:
        return (
            False,
            f"出现了 {sorted(illegal)} 这一类。\n"
            "SNMPv2c 没有「认证失败」这个错误 —— community 错就是超时，"
            "设备连包都不回。别在分类里编协议不存在的东西。",
        )
    get_kinds = {f["kind"] for f in failures if f["stage"] != "ifTable"}
    if get_kinds != {"timeout"}:
        return (
            False,
            f"三个 GET 项（sysDescr/uptime/sysName）应全是 timeout，实际 {sorted(get_kinds)}\n"
            "community 错时设备静默丢包，客户端只能看到「没应答」。",
        )
    if value["verdict"] != "FAILED" or value["exit_code"] != 1:
        return (
            False,
            f"四项全失败应是 FAILED / 退出码 1，实际 {value['verdict']} / {value['exit_code']}",
        )
    note = (
        "（ifTable 记成 protocol 是 D10 封装的遗留，根因丢了 —— 见 probe.py 里的注释）"
        if "protocol" in kinds
        else ""
    )
    return True, f"3 个 GET 项全 TIMEOUT，verdict=FAILED，退出码 1{note}"


def check_c5(ports: dict[str, int]) -> tuple[bool, str]:
    got = run("collect", "127.0.0.1", str(ports["errs"]), "public", "0.5", timeout=90)
    if not got.get("ok"):
        return False, f"collect_device 没跑通。{describe(got)}"
    value = got["value"]
    by_stage = {f["stage"]: f for f in value["failures"]}
    if "sysDescr" not in by_stage or by_stage["sysDescr"]["kind"] != "device_error":
        return (
            False,
            f"sysDescr 收到 genErr(5)，应归 device_error，实际 {by_stage.get('sysDescr')}\n"
            "status 除了 1(tooBig)、2(noSuchName) 之外都是 device_error。",
        )
    if "sysName" not in by_stage or by_stage["sysName"]["kind"] != "bad_oid":
        return (
            False,
            f"sysName 收到 noSuchName(2)，应归 bad_oid，实际 {by_stage.get('sysName')}",
        )
    for stage in ("uptime", "ifTable"):
        if stage not in value["values"]:
            return (
                False,
                f"{stage} 应该照常采到，实际不在 values 里（values={list(value['values'])}）\n"
                "两项失败不能把另外两项也拖下水 —— 这就是「部分成功」。",
            )
    if value["verdict"] != "DEGRADED" or value["exit_code"] != 2:
        return (
            False,
            f"2 项成功 2 项失败应是 DEGRADED / 退出码 2，"
            f"实际 {value['verdict']} / {value['exit_code']}",
        )
    if value["total"] != 4:
        return False, f"total 应是 4，实际 {value['total']}"
    return True, "device_error + bad_oid 各就各位，另两项照常采到，DEGRADED / 退出码 2"


def check_c6(ports: dict[str, int]) -> tuple[bool, str]:
    got = run("collect", "127.0.0.1", str(ports["toobig"]), "public", "0.5", timeout=90)
    if not got.get("ok"):
        return False, f"collect_device 没跑通。{describe(got)}"
    value = got["value"]
    by_stage = {f["stage"]: f for f in value["failures"]}
    if "ifTable" not in by_stage:
        return (
            False,
            f"ifTable 收到 tooBig(1) 却没被记成失败（failures={list(by_stage)}）\n"
            "walk 抛的 ProbeError 要在 collect_device 里接住，归到 PROTOCOL。",
        )
    if by_stage["ifTable"]["kind"] != "protocol":
        return (
            False,
            f"walk 的失败只能归 PROTOCOL（D10 的封装没保留 error_status），"
            f"实际 {by_stage['ifTable']['kind']!r}",
        )
    if len(value["failures"]) != 1:
        return (
            False,
            f"只有 ifTable 一列该失败，实际 {len(value['failures'])} 条失败："
            f"{[f['stage'] for f in value['failures']]}\n"
            "tooBig 是 ifDescr 那一次 WALK 的事，不该拖累三个 GET 项。",
        )
    for stage in ("sysDescr", "uptime", "sysName"):
        if stage not in value["values"]:
            return False, f"{stage} 应该不受影响，实际不在 values 里"
    if value["verdict"] != "DEGRADED":
        return False, f"1 项失败 3 项成功应是 DEGRADED，实际 {value['verdict']}"
    return True, "ifTable 单列 tooBig 被接住，前 3 项完好，DEGRADED"


def check_c7(ports: dict[str, int]) -> tuple[bool, str]:
    proc = run_cli(ports["normal"])
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, f"正常设备退出码应是 0，实际 {proc.returncode}\n{tail(out)}"
    for stage in STAGES:
        if stage not in out:
            return False, f"输出里没有 {stage}：\n{tail(out)}"
    if "OK" not in out:
        return False, f"输出里没有 OK：\n{tail(out)}"
    return True, "退出码 0，4 个项目 + OK 都打出来了"


CHECKS = [
    ("C1", "classify：8 组输入 → 8 个结果", check_c1, True, False),
    ("C2", "get_raw 成功路径", check_c2, True, True),
    ("C3", "get_raw 失败：不抛异常 + TIMEOUT + 不重试", check_c3, True, True),
    ("C4", "community 错 → TIMEOUT（不是 auth）+ FAILED", check_c4, True, True),
    ("C5", "部分成功：genErr + noSuchName", check_c5, True, True),
    ("C6", "tooBig 只拖垮 ifTable 一列", check_c6, True, True),
    ("C7", "python probe.py --port ...", check_c7, False, True),
]


def ready() -> tuple[bool, str]:
    missing = [
        n
        for n in ("probe.py", "ifwalk.py", "snmpio.py", "fake_agent.py")
        if not (ROOT / n).exists()
    ]
    if missing:
        return False, f"缺文件：{', '.join(missing)}"
    src = ENTRY.read_text(encoding="utf-8")
    todo = len(re.findall(r"^\s*raise NotImplementedError", src, re.M))
    if todo:
        return False, f"probe.py 里还有 {todo} 个 raise NotImplementedError —— TODO 没写完"
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
    return True, f"probe.py 的 TODO 已全部填完；pysnmp 可用（{interpreter()}）"


def main() -> int:
    print("=" * 64)
    print("D12 验收：错误分类 + 部分成功")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")

    ok_ready, info = ready()
    print(("准备：" + info) if ok_ready else ("准备：[!] " + info))

    took: set[int] = set()
    ports = {
        "normal": free_port(took),
        "errs": free_port(took),
        "toobig": free_port(took),
    }
    agents: list[subprocess.Popen[bytes]] = []
    all_alive = False
    if ok_ready:
        agents = [
            start_agent(ports["normal"]),
            start_agent(ports["errs"], "--gen-err", "--no-such-name"),
            start_agent(ports["toobig"], "--too-big"),
        ]
        alive = {name: wait_agent(port) for name, port in ports.items()}
        all_alive = all(alive.values())
        table = "　".join(f"{name} {ports[name]}{'' if alive[name] else ' [!]'}" for name in ports)
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
        print("结论：还没全绿。顺序建议：C1（判据顺序）→ C2（成功路径）")
        print("      → C3（不抛异常 + retries=0）→ C4（v2c 没有 auth）")
        print("      → C5/C6（部分成功、不变量 total==4）。")
    else:
        print("结论：全绿。失败现在是数据了 —— D13 把 host 列表也搬进配置文件，")
        print("      让同一套代码扫一整批设备。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
