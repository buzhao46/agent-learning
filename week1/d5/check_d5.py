"""D5 自动验收：7 项，跑它就知道你的测试写对了没有。

    cd week1/d5
    python check_d5.py

退出码 0 = 必做项全过；非 0 = 还有 ❌。

这个脚本不看你写了几个测试函数，而是**故意把你被测的代码改坏**，然后看你的测试能不能发现。
这叫「变异测试」（mutation testing）——它是唯一能证明"测试真的有用"的办法。

只有 assert True 的测试、只跑不断言的测试、永远通过的测试，在这里全部会红。
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).parent
MODELS = ROOT / "models.py"

TEST_GLOBS = ("test_*.py", "*_test.py")

# 变异留下的指纹。用来发现「上次检查被 Ctrl+C / 超时中断，models.py 没恢复」。
FINGERPRINTS = (
    "except ZeroDivisionError",
    "if False:",
    'return {"name": self.name, "ip": self.ip}',
)


# ---------------------------------------------------------------- 工具
def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def test_files() -> list[Path]:
    found: list[Path] = []
    for pattern in TEST_GLOBS:
        found.extend(p for p in ROOT.glob(pattern) if p.name != "check_d5.py")
    return sorted(set(found))


def run_pytest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env(),
        timeout=120,
        check=False,
    )


def pytest_ready() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env(),
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        return False, "当前 Python 里没有 pytest"
    return True, (proc.stdout or "").strip()


def clear_cache() -> None:
    """删掉 __pycache__ 和 pytest 缓存，避免变异后跑到旧字节码。"""
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    for junk in (".pytest_cache",):
        shutil.rmtree(ROOT / junk, ignore_errors=True)


_BASELINE: tuple[bool, str] | None = None


def baseline_green() -> tuple[bool, str]:
    """变异测试的前提：原始代码下测试必须全绿。

    否则「改坏后测试失败」根本不能证明你的测试抓到了 bug —— 它可能本来就红。
    这是变异测试最容易被忽略的假阳性来源。
    """
    global _BASELINE
    if _BASELINE is None:
        clear_cache()
        proc = run_pytest("-q")
        if proc.returncode == 0:
            _BASELINE = (True, "基线全绿")
        else:
            lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
            _BASELINE = (False, lines[-1] if lines else "pytest 执行失败")
    return _BASELINE


def source_ok() -> tuple[bool, str]:
    """被测代码和测试文件都在，且没有上次中断留下的变异。"""
    missing = [n for n in ("models.py", "log_setup.py") if not (ROOT / n).exists()]
    if missing:
        return False, f"缺文件：{', '.join(missing)} —— 先把 D4 的三个文件复制到 d5/"

    left = [f for f in FINGERPRINTS if f in MODELS.read_text(encoding="utf-8")]
    if left:
        return False, (
            f"models.py 里有上次检查中断留下的改动（{left[0]!r}）。\n"
            "       从 d4 复制一份干净的回来：Copy-Item ..\\d4\\models.py models.py"
        )

    tests = test_files()
    if not tests:
        return False, "没找到测试文件。写一个 test_device.py（或 test_models.py）"
    return True, f"被测代码就位；测试文件：{', '.join(p.name for p in tests)}"


# ---------------------------------------------------------------- 检查项
def check_collect_and_green() -> tuple[bool, str]:
    """pytest 能跑通，且至少 5 个用例，全部通过。"""
    ok, msg = pytest_ready()
    if not ok:
        return False, (
            f"{msg}。先激活虚拟环境再跑：\n"
            "       .venv\\Scripts\\activate      （在 week1 目录下执行）\n"
            "       然后 cd d5 && python check_d5.py"
        )

    proc = run_pytest("--collect-only", "-q")
    ids = [ln for ln in (proc.stdout or "").splitlines() if "::" in ln]
    if len(ids) < 5:
        return False, f"只收集到 {len(ids)} 个用例，至少要有 5 个（正常 / 未知设备 / 空列表 / CPU 边界 / 文件不存在）"

    proc = run_pytest("-q")
    if proc.returncode != 0:
        tail = (proc.stdout or "").strip().splitlines()
        fail_line = next((ln for ln in reversed(tail) if "passed" in ln or "failed" in ln or "error" in ln), "")
        detail = fail_line or (tail[-1] if tail else "无输出")
        return False, f"有测试没通过：{detail}（先让全部变绿，再看其它项）"

    return True, f"pytest 收集到 {len(ids)} 个用例，全部通过"


def check_real_asserts() -> tuple[bool, str]:
    """每个 test_ 函数里都要有真正的断言，不能是空壳。"""
    weak: list[str] = []
    total = 0
    for path in test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test"):
                continue
            total += 1
            if not has_real_assert(node):
                weak.append(f"{path.name}::{node.name}")
    if total == 0:
        return False, "测试文件里一个 test_ 函数都没有"
    if weak:
        return False, f"这些用例没有任何断言，永远会通过：{', '.join(weak)}"
    return True, f"{total} 个用例都有真实断言"


def has_real_assert(func: ast.FunctionDef) -> bool:
    """函数体里要有 assert，或者 with pytest.raises(...) —— 后者本身就是断言。"""
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in ("raises", "warns", "fail"):
                return True
    return False


def mutate(label: str, pattern: str, repl: str, why: str) -> tuple[bool, str]:
    """把被测代码改坏成 `why`，看测试会不会红。红 = 抓住了。"""
    if not MODELS.exists():
        return False, "models.py 不存在，先做 C1"

    ok, msg = baseline_green()
    if not ok:
        return False, f"基线测试本身就没过（{msg}）—— 先让 C1 全绿，变异结论才可信"

    backup = MODELS.read_text(encoding="utf-8")
    new, count = re.subn(pattern, repl, backup, count=1)
    if count == 0:
        return False, (
            f"没找到变异位置（我的正则：{pattern}）。"
            "你的实现写法可能和 D4 参考版不同 —— 请自查这个场景有没有测试覆盖"
        )

    MODELS.write_text(new, encoding="utf-8")
    try:
        clear_cache()
        proc = run_pytest("-q", "-x")
    finally:
        MODELS.write_text(backup, encoding="utf-8")
        clear_cache()

    if proc.returncode == 0:
        return False, (
            f"我把代码改坏成「{why}」，你的测试**依然全绿**。\n"
            "       说明这个场景没测到，或者断言太松，测了等于没测"
        )
    return True, f"改坏成「{why}」→ 测试立刻变红，抓到了"


def check_boundary() -> tuple[bool, str]:
    """边界：cpu == cpu_limit 时应判为不健康（代码用的是 <，不是 <=）。"""
    return mutate(
        "CPU 边界",
        r"self\.cpu\s*<\s*cpu_limit",
        "self.cpu <= cpu_limit",
        "cpu == cpu_limit 时算健康",
    )


def check_invalid_status() -> tuple[bool, str]:
    """校验：status 不是 up/down 时必须抛 SnmpError。"""
    return mutate(
        "非法 status",
        r'if\s+status\s+not\s+in\s*\(\s*["\']up["\']\s*,\s*["\']down["\']\s*\)\s*:',
        "if False:",
        "非法状态也放行、不抛错",
    )


def check_file_missing() -> tuple[bool, str]:
    """文件不存在时必须抛 SnmpError，而不是裸 FileNotFoundError。"""
    return mutate(
        "文件不存在",
        r"except\s+FileNotFoundError",
        "except ZeroDivisionError",
        "文件不存在时不转成 SnmpError",
    )


def check_roundtrip() -> tuple[bool, str]:
    """序列化往返：to_dict 漏字段时必须被发现（用默认值的数据测不出来）。"""
    return mutate(
        "to_dict 漏字段",
        r"return\s+asdict\(self\)",
        'return {"name": self.name, "ip": self.ip}',
        "to_dict 只返回 name/ip",
    )


def check_tmp_path() -> tuple[bool, str]:
    """【加练】用 pytest 的 tmp_path，测试不往仓库目录里写文件。"""
    for path in test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                names = [a.arg for a in node.args.args]
                if "tmp_path" in names or "tmp_path_factory" in names:
                    return True, f"{path.name}::{node.name} 用了 tmp_path 夹具"
    return False, (
        "没有用例使用 tmp_path。测「写文件」时不要写到当前目录，"
        "用 tmp_path 让 pytest 给你一个临时目录"
    )


CHECKS: list[tuple[str, str, Callable[[], tuple[bool, str]], bool]] = [
    ("C1 pytest 跑通", "至少 5 个用例且全绿", check_collect_and_green, True),
    ("C2 断言是真的", "没有空壳用例", check_real_asserts, True),
    ("C3 变异·CPU 边界", "cpu == 上限时的判定", check_boundary, True),
    ("C4 变异·非法 status", "非 up/down 要抛错", check_invalid_status, True),
    ("C5 变异·文件不存在", "要转成 SnmpError", check_file_missing, True),
    ("C6 变异·序列化往返", "to_dict 缺字段要能发现", check_roundtrip, True),
    ("C7 加练·tmp_path", "写文件不污染仓库", check_tmp_path, False),
]


def main() -> int:
    print("=" * 64)
    print("D5 验收：第一次写 pytest")
    print("=" * 64)

    ready, info = pytest_ready()
    print(f"\n环境：{info}" if ready else f"\n环境：⚠️  {info}")

    ok, msg = source_ok()
    if not ok:
        print(f"\n❌ 准备检查没过：\n    {msg}")
        print("\n" + "-" * 64)
        print("把上面这条解决后，再重跑：python check_d5.py")
        return 1
    print(f"    {msg}")

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
        print("结论：C1 必须先绿（pytest 跑不通，后面全是空谈）。")
        print("      C3–C6 是四个变异，每个对应一个「以为测了、其实没测」的角落。")
    else:
        print("结论：全绿。你写的测试是**真的能抓 bug** 的测试，不是摆设。D5 完成。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
