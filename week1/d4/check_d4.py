"""D4 自动验收：7 项，跑它就知道拆分拆对了没有。

    cd week1/d4
    python check_d4.py

退出码 0 = 必做项全过；非 0 = 还有 ❌。

考核重点不是"能跑"，而是三件事：
  1. 拆得干净（谁都不许反向 import 自己的上游）
  2. 行为不变（重构前后输出必须一模一样 —— 这是重构的唯一验收标准）
  3. 能复用（models.py 能被别的程序单独 import，且 import 时没有副作用）
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).parent
D3 = ROOT.parent / "d3"
LOG_FILE = ROOT / "out" / "run.log"

MODELS = ROOT / "models.py"
LOG_SETUP = ROOT / "log_setup.py"
CLI = ROOT / "cli.py"

TS = re.compile(r"^\d{2}:\d{2}:\d{2} \| ")


# ---------------------------------------------------------------- 工具
def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env(),
        timeout=30,
        check=False,
    )


def reset_log() -> None:
    if LOG_FILE.exists():
        LOG_FILE.unlink()


def log_lines(path: Path) -> list[str]:
    """读 run.log 并抹掉时间戳，只保留「级别 | 名字 | 正文」便于比较。"""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [TS.sub("", ln) for ln in text.splitlines() if ln.strip()]


def top_imports(path: Path) -> set[str]:
    """用 AST 抓出文件里所有 import 的顶层模块名（注释、字符串里的不算）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def has_traceback(proc: subprocess.CompletedProcess[str]) -> bool:
    return "Traceback (most recent call last)" in (proc.stderr or "")


# ---------------------------------------------------------------- 检查项
def check_files() -> tuple[bool, str]:
    """三个文件都得有，且各司其职。"""
    missing = [p.name for p in (MODELS, LOG_SETUP, CLI) if not p.exists()]
    if missing:
        return False, f"缺文件：{', '.join(missing)}（当前目录只有：{sorted(x.name for x in ROOT.glob('*.py'))}）"
    empty = [p.name for p in (MODELS, LOG_SETUP, CLI) if len(p.read_text(encoding="utf-8").strip()) < 80]
    if empty:
        return False, f"这些文件几乎是空的：{', '.join(empty)} —— 是从 loader.py 搬过来的吗？"
    return True, "models.py / log_setup.py / cli.py 都在，且都有内容"


def check_models_pure() -> tuple[bool, str]:
    """models.py 只放数据模型和读写，不许掺命令行、不许直接 print。"""
    if not MODELS.exists():
        return False, "models.py 不存在，先做 C1"
    bad = top_imports(MODELS) & {"argparse", "sys", "logging"}
    if bad:
        return False, f"models.py 不该 import {', '.join(sorted(bad))} —— 它只负责数据，不掺命令行和日志配置"
    src = MODELS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            return False, f"models.py 第 {node.lineno} 行有 print(...) —— 数据层不该直接往屏幕输出"
    return True, "models.py 只依赖标准数据工具，没有 argparse / logging / print"


def check_no_reverse_import() -> tuple[bool, str]:
    """谁都不许反向依赖入口，否则迟早循环导入。"""
    problems = []
    if MODELS.exists() and "cli" in top_imports(MODELS):
        problems.append("models.py 反过来 import 了 cli")
    if LOG_SETUP.exists():
        imp = top_imports(LOG_SETUP)
        if "cli" in imp:
            problems.append("log_setup.py 反过来 import 了 cli")
    if problems:
        return False, "；".join(problems) + " —— 依赖方向只能是 cli → models / log_setup，不能倒过来"
    return True, "依赖方向正确：cli 依赖下层，下层不认识 cli"


def check_behaviour_same() -> tuple[bool, str]:
    """重构的唯一验收标准：同样的参数，输出必须和 D3 一模一样。"""
    if not CLI.exists():
        return False, "cli.py 不存在，先做 C1"
    if not (D3 / "loader.py").exists():
        return False, "找不到 ../d3/loader.py，没法做对照"

    args = ("--limit", "2", "--verbose")

    d3_log = D3 / "out" / "run.log"
    if d3_log.exists():
        d3_log.unlink()
    reset_log()
    old = run(D3, "loader.py", *args)
    old_out, old_log = old.stdout or "", log_lines(d3_log)
    reset_log()
    new = run(ROOT, "cli.py", *args)
    new_out, new_log = new.stdout or "", log_lines(LOG_FILE)

    if new.returncode != 0:
        tail = (new.stderr or "").strip().splitlines()
        return False, f"python cli.py {' '.join(args)} 退出码 {new.returncode}；{tail[-1] if tail else '无输出'}"

    if old_out != new_out:
        return False, (
            "stdout 和 D3 不一致 —— 重构不能改变输出。\n"
            f"       D3 共 {len(old_out.splitlines())} 行，你的 {len(new_out.splitlines())} 行"
        )
    if old_log != new_log:
        diff = [f"D3={a!r} 你的={b!r}" for a, b in zip(old_log, new_log) if a != b]
        return False, f"run.log 和 D3 不一致（{len(old_log)} vs {len(new_log)} 行）" + (
            f"；第一处差异：{diff[0]}" if diff else ""
        )
    return True, f"输出与 D3 逐字节一致（stdout {len(new_out.splitlines())} 行 + 日志 {len(new_log)} 行）"


def check_reusable() -> tuple[bool, str]:
    """models.py 能被别的程序单独用 —— 这就是拆分的意义。"""
    if not MODELS.exists():
        return False, "models.py 不存在"
    code = (
        "from models import Device, SnmpError\n"
        "d = Device.from_dict({'name': 'x', 'ip': '1.1.1.1', 'status': 'up', 'cpu': 30})\n"
        "print('health', d.is_healthy())\n"
        "print('round', Device.from_dict(d.to_dict()).name)\n"
        "try:\n"
        "    Device.from_dict({'name': 'y', 'ip': '1.1.1.2', 'status': 'broken'})\n"
        "except SnmpError:\n"
        "    print('guard ok')\n"
    )
    proc = run(ROOT, "-c", code)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return False, f"单独 import models 用不了：{tail[-1] if tail else '无输出'}"
    out = (proc.stdout or "").strip()
    for token in ("health True", "round x", "guard ok"):
        if token not in out:
            return False, f"期望输出里有 {token!r}，实际：{out}"
    return True, "models.py 可以被外部单独 import：构造 / 序列化 / 校验都正常"


def check_no_side_effect() -> tuple[bool, str]:
    """import models 只是拿到类，不该建日志、写文件、打印任何东西。"""
    if not MODELS.exists():
        return False, "models.py 不存在"
    reset_log()
    proc = run(ROOT, "-c", "import models")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return False, f"import models 就报错了：{tail[-1] if tail else '无输出'}"
    if (proc.stdout or "").strip():
        return False, f"光是 import 就往 stdout 打了东西：{proc.stdout!r}"
    if LOG_FILE.exists():
        return False, "光是 import models 就创建了 out/run.log —— 日志应该延迟到真正要写时才初始化"
    return True, "import models 零副作用：不打印、不建日志文件"


def check_module_entry() -> tuple[bool, str]:
    """【加练】python -m cli 也能跑（不靠文件路径，靠模块名）。"""
    if not CLI.exists():
        return False, "cli.py 不存在"
    reset_log()
    proc = run(ROOT, "-m", "cli", "--limit", "1")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return False, f"python -m cli 退出码 {proc.returncode}；{tail[-1] if tail else '无输出'}"
    if "core-sw-01" not in (proc.stdout or ""):
        return False, "python -m cli --limit 1 的输出里没有 core-sw-01"
    if "agg-sw-03" in (proc.stdout or ""):
        return False, "--limit 1 却输出了多台"
    return True, "python -m cli 同样可用"


CHECKS: list[tuple[str, str, Callable[[], tuple[bool, str]], bool]] = [
    ("C1 三个文件就位", "models.py / log_setup.py / cli.py", check_files, True),
    ("C2 models 职责纯净", "不掺 argparse / logging / print", check_models_pure, True),
    ("C3 无反向依赖", "下层不认识 cli", check_no_reverse_import, True),
    ("C4 行为不变", "输出与 D3 逐字节一致", check_behaviour_same, True),
    ("C5 models 可复用", "外部能单独 import 使用", check_reusable, True),
    ("C6 import 无副作用", "不打印、不建日志文件", check_no_side_effect, True),
    ("C7 加练·模块入口", "python -m cli 也能跑", check_module_entry, False),
]


def main() -> int:
    print("=" * 64)
    print("D4 验收：拆成 models.py + log_setup.py + cli.py")
    print("=" * 64)

    passed = required_failed = optional_failed = 0
    for label, detail, fn, required in CHECKS:
        try:
            ok, msg = fn()
        except Exception as exc:  # noqa: BLE001 - 验收脚本自己不能崩
            ok, msg = False, f"验收时抛异常：{type(exc).__name__}: {exc}"
        mark = "✅" if ok else ("❌" if required else "⚠️ ")
        tag = "" if required else "　[加练]"
        print(f"\n{mark} {label}{tag}")
        print(f"    {detail}")
        if not ok:
            print(f"    → {msg}")
        if ok:
            passed += 1
        elif required:
            required_failed += 1
        else:
            optional_failed += 1

    total = len(CHECKS)
    print("\n" + "-" * 64)
    print(f"通过 {passed}/{total}　必做失败 {required_failed}　加练失败 {optional_failed}")
    if required_failed:
        print("结论：先看第一个 ❌ 的那条建议，改完重跑。C4 通常最后才过。")
    else:
        print("结论：全绿。D4 完成 —— 你已经能把一坨脚本拆成有层次的小项目了。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
