"""D7 验收：合成 snmpcheck（subparsers 与子命令分发）。

七项：
  C1  list 子命令能跑，只列清单、不带耗时
  C2  status 子命令能跑，带耗时
  C3  不写子命令时报错退出 2（required=True 生效）
  C4  --help 能列出 list / status 两个子命令
  C5  list --limit 2 参数生效
  C6  status --attempts 1 参数真的传下去了
  C7  [加练] python -m snmpcheck list 也能跑
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "snmpcheck.py"
DEVICES_FILE = ROOT / "devices.json"

VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"


def interpreter() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def make_env() -> dict[str, str]:
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e["PYTHONUTF8"] = "1"
    e["PYTHONPATH"] = str(ROOT) + os.pathsep + e.get("PYTHONPATH", "")
    return e


def run(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """跑 `python snmpcheck.py <args>`。"""
    return subprocess.run(
        [interpreter(), "snmpcheck.py", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=make_env(),
        timeout=timeout,
    )


def run_module(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """跑 `python -m snmpcheck <args>`。"""
    return subprocess.run(
        [interpreter(), "-m", "snmpcheck", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=make_env(),
        timeout=timeout,
    )


def tail(text: str | None, n: int = 600) -> str:
    body = (text or "").strip()
    if not body:
        return "(空)"
    return "\n".join(body.splitlines()[-8:])[:n]


def device_names() -> list[str]:
    data = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
    return [d["name"] for d in data["devices"]]


def rows_of(stdout: str) -> list[str]:
    """从表格里挑出设备行（以设备名开头）。"""
    names = device_names()
    return [ln for ln in stdout.splitlines() if any(ln.startswith(n) for n in names)]


def ready() -> tuple[bool, str]:
    need = ("snmpcheck.py", "models.py", "log_setup.py", "probe.py", "resilience.py", "devices.json")
    missing = [n for n in need if not (ROOT / n).exists()]
    if missing:
        return False, f"缺文件：{', '.join(missing)}"
    src = ENTRY.read_text(encoding="utf-8")
    if re.search(r"^\s*raise NotImplementedError", src, re.M):
        return False, "snmpcheck.py 里还有 raise NotImplementedError —— 四个函数还没写完"
    return True, "六个文件就位，snmpcheck.py 已无占位"


# ============================================================ 七项检查
def check_c1() -> tuple[bool, str]:
    proc = run(["list"])
    if proc.returncode != 0:
        return False, f"python snmpcheck.py list 退出码 {proc.returncode}\nstderr：\n{tail(proc.stderr)}"
    if "设备" not in proc.stdout:
        return False, "stdout 里没有表格表头，看起来 print_table 没被调用\nstdout：\n" + tail(proc.stdout)

    rows = rows_of(proc.stdout)
    if len(rows) != len(device_names()):
        return False, f"应列出 {len(device_names())} 台设备，实际 {len(rows)} 行\nstdout：\n{tail(proc.stdout)}"

    with_ms = [r for r in rows if re.search(r"\d+\.\d+ms", r)]
    if with_ms:
        return False, (
            "list 不该有耗时列（它不采集）。下面这些行带了耗时：\n" + "\n".join(with_ms)
            + "\n（cmd_list 里应该 print_table(picked) 只传一个参数）"
        )
    return True, f"列出 {len(rows)} 台，无耗时列（正确，list 不采集）"


def check_c2() -> tuple[bool, str]:
    proc = run(["status"])
    if proc.returncode != 0:
        return False, f"python snmpcheck.py status 退出码 {proc.returncode}\nstderr：\n{tail(proc.stderr)}"

    rows = rows_of(proc.stdout)
    if len(rows) != len(device_names()):
        return False, f"应输出 {len(device_names())} 台设备，实际 {len(rows)} 行\nstdout：\n{tail(proc.stdout)}"

    no_ms = [r for r in rows if not re.search(r"\d+\.\d+ms", r)]
    if no_ms:
        return False, (
            "status 的表格缺少耗时列。下面这些行没有：\n" + "\n".join(no_ms)
            + "\n（cmd_status 里要把 {设备名: 秒数} 传给 print_table 的第二个参数）"
        )
    return True, "status 输出 3 台，每台都有耗时"


def check_c3() -> tuple[bool, str]:
    proc = run([])
    if proc.returncode == 0:
        return False, (
            "不带子命令执行时退出码是 0 —— 说明 add_subparsers 少了 required=True。\n"
            "后果：用户敲错命令，程序静默什么都不做，还以为成功了。"
        )
    if proc.returncode != 2:
        return False, f"不带子命令时应以退出码 2 结束（argparse 的约定），实际 {proc.returncode}\nstderr：\n{tail(proc.stderr)}"
    blob = (proc.stderr or "") + (proc.stdout or "")
    if "list" not in blob or "status" not in blob:
        return False, f"报错信息里没有列出可用的子命令：\n{tail(blob)}"
    return True, "不带子命令 → 退出码 2，并提示需要 command"


def check_c4() -> tuple[bool, str]:
    proc = run(["--help"])
    if proc.returncode != 0:
        return False, f"--help 退出码 {proc.returncode}\nstderr：\n{tail(proc.stderr)}"
    text = (proc.stdout or "") + (proc.stderr or "")
    missing = [name for name in ("list", "status") if name not in text]
    if missing:
        return False, f"--help 里看不到 {', '.join(missing)}\n实际输出：\n{tail(text)}"
    if "{" not in text:
        return False, "--help 里没有出现 {list,status} 这样的选择列表，子命令可能没注册到 subparsers 上"
    return True, "--help 列出了 list 与 status"


def check_c5() -> tuple[bool, str]:
    proc = run(["list", "--limit", "2"])
    if proc.returncode != 0:
        return False, f"list --limit 2 退出码 {proc.returncode}\nstderr：\n{tail(proc.stderr)}"
    rows = rows_of(proc.stdout)
    if len(rows) != 2:
        return False, (
            f"list --limit 2 应输出 2 台，实际 {len(rows)} 行。\n"
            "两种可能：① --limit 没加在 list 子命令上；② load_and_filter 没被调用\n"
            "stdout：\n" + tail(proc.stdout)
        )
    return True, "list --limit 2 → 2 台"


def check_c6() -> tuple[bool, str]:
    base = run(["status"])
    if base.returncode != 0:
        return False, f"status 都跑不通，先过 C2\nstderr：\n{tail(base.stderr)}"
    base_fail = (base.stderr or "").count("采集失败")

    one = run(["status", "--attempts", "1"])
    if "unrecognized arguments" in (one.stderr or ""):
        return False, (
            "status 不认识 --attempts。注意：参数要加在 **status 子命令**上，"
            "加在顶层 parser 上的话必须写成 `snmpcheck.py --attempts 1 status`"
        )
    if one.returncode != 0:
        return False, f"status --attempts 1 退出码 {one.returncode}\nstderr：\n{tail(one.stderr)}"
    one_fail = (one.stderr or "").count("采集失败")

    if base_fail != 0:
        return False, f"默认 status 不应有采集失败（attempts 默认 3，3 台设备最多抖 2 次），实际 {base_fail} 台失败"
    if one_fail != 2:
        return False, (
            f"status --attempts 1 应有 2 台失败（第 2、3 台各要抖一次），实际 {one_fail} 台。\n"
            "说明 --attempts 的值没传进 collect_all"
        )
    return True, f"默认 0 台失败 → --attempts 1 时 {one_fail} 台失败，参数确实传下去了"


def check_c7() -> tuple[bool, str]:
    proc = run_module(["list"])
    if proc.returncode != 0:
        return False, (
            f"python -m snmpcheck list 退出码 {proc.returncode}\nstderr：\n{tail(proc.stderr)}\n"
            "（-m 方式要求 snmpcheck.py 在同目录，并且用不带点的绝对导入）"
        )
    rows = rows_of(proc.stdout)
    if len(rows) != len(device_names()):
        return False, f"python -m snmpcheck list 应列出 {len(device_names())} 台，实际 {len(rows)} 行"
    return True, "python -m snmpcheck list 也能跑（D19 打包前的预习）"


CHECKS: list[tuple[str, str, object, bool]] = [
    ("C1", "list 子命令：只列清单不带耗时", check_c1, True),
    ("C2", "status 子命令：采集并带耗时", check_c2, True),
    ("C3", "不带子命令报错退出 2", check_c3, True),
    ("C4", "--help 列出两个子命令", check_c4, True),
    ("C5", "list --limit 2 生效", check_c5, True),
    ("C6", "status --attempts 1 生效", check_c6, True),
    ("C7", "python -m snmpcheck list", check_c7, False),
]


def main() -> int:
    print("=" * 64)
    print("D7 验收：合成 snmpcheck（subparsers）")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")

    ok_ready, info = ready()
    print(f"准备：{info}" if ok_ready else f"准备：⚠️  {info}")

    passed = required_failed = optional_failed = 0
    for label, desc, fn, required in CHECKS:
        print(f"\n{label}  {desc}" + ("" if required else "　[加练]"))
        if not ok_ready:
            print("    ⏭  跳过（准备检查没过）")
            if required:
                required_failed += 1
            else:
                optional_failed += 1
            continue
        try:
            good, message = fn()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - 验收脚本自己绝不能崩
            good, message = False, f"验收时抛异常：{type(exc).__name__}: {exc}"
        print(f"    {'✅' if good else '❌'} {message}")
        if good:
            passed += 1
        elif required:
            required_failed += 1
        else:
            optional_failed += 1

    total = len(CHECKS)
    print("\n" + "-" * 64)
    print(f"通过 {passed}/{total}　必做失败 {required_failed}　加练失败 {optional_failed}")
    if required_failed:
        print("结论：还没到 7/7。建议顺序：C3/C4（先把解析器搭对）→ C1 → C2 → C5 → C6。")
    else:
        print("结论：全绿。snmpcheck 成型了 —— 这是 21 天训练第 1 周的收官，也是你第一个能写进简历的东西。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
