"""D1 自动验收：跑它就知道过没过，不用肉眼对比。

    cd week1/d1
    python check_d1.py

退出码 0 = 必做项全过；非 0 = 还有 ❌。
"""
from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from pathlib import Path

from loader import DEFAULT_FILE, Device, SnmpError, load_devices

# day1_types.py 里 SEED 的原始值，这就是「跑出同样结果」的基准
EXPECTED: list[tuple[str, str, str, int]] = [
    ("core-sw-01", "10.0.0.1", "up", 92),
    ("core-rtr-02", "10.0.0.2", "up", 41),
    ("agg-sw-03", "10.0.0.3", "down", -1),
]


def _result(devices: list[Device]) -> list[tuple[str, str, str, int]]:
    return [(d.name, d.ip, d.status, d.cpu) for d in devices]


def c_json_parsable() -> tuple[bool, str]:
    raw = json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
    return True, f"devices.json 可解析，{len(raw['devices'])} 条记录"


def c_load_count() -> tuple[bool, str]:
    devices = load_devices()
    return (len(devices) == 3), f"load_devices() 返回 {len(devices)} 台（期望 3）"


def c_same_as_seed() -> tuple[bool, str]:
    got = _result(load_devices())
    if got == EXPECTED:
        return True, "字段与顺序完全一致（name/ip/status/cpu）"
    return False, f"不一致\n      期望 {EXPECTED}\n      实际 {got}"


def c_tags_loaded() -> tuple[bool, str]:
    devices = load_devices()
    empty = [d.name for d in devices if not d.tags]
    return (not empty), f"tags 读取正常（{devices[0].tags if devices else '?'}）" if not empty else f"这些设备 tags 丢了：{empty}"


def c_missing_file() -> tuple[bool, str]:
    try:
        load_devices(Path("definitely-not-here.json"))
    except SnmpError as exc:
        return True, f"文件不存在时抛 SnmpError：{exc}"
    except FileNotFoundError:
        return False, "抛的是 FileNotFoundError —— 记得包成 SnmpError（见 docstring 步骤 1-b）"
    return False, "居然没报错？文件不存在必须 raise SnmpError"


def c_bad_status() -> tuple[bool, str]:
    """加练项：非法 status 应该被拦住。"""
    try:
        Device.from_dict({"name": "x", "ip": "1.1.1.1", "status": "UP"})
    except SnmpError:
        return True, "非法 status 已拦住"
    return False, 'status="UP" 被放行了（加练项，见 from_dict 的 TODO）'


CHECKS: list[tuple[str, bool, Callable[[], tuple[bool, str]]]] = [
    ("devices.json 存在且是合法 JSON", True, c_json_parsable),
    ("load_devices() 能读到 3 台设备", True, c_load_count),
    ("结果与 day1_types.py 的 SEED 完全一致", True, c_same_as_seed),
    ("tags 字段没丢", True, c_tags_loaded),
    ("文件不存在时抛 SnmpError", True, c_missing_file),
    ("加练：非法 status 抛 SnmpError", False, c_bad_status),
]


def run_one(fn: Callable[[], tuple[bool, str]]) -> tuple[bool, str]:
    try:
        return fn()
    except NotImplementedError:
        return False, "还没实现 → 打开 loader.py，补 load_devices()"
    except Exception:  # noqa: BLE001 - 验收脚本要兜住任何崩栈并给出最后一行
        return False, traceback.format_exc().strip().splitlines()[-1]


def main() -> int:
    print("=" * 60)
    print("D1 验收：数据外置（SEED -> devices.json）")
    print("=" * 60)

    required_failed = 0
    optional_failed = 0
    for label, required, fn in CHECKS:
        ok, detail = run_one(fn)
        mark = "✓" if ok else ("✗" if required else "○")
        tag = "" if required else "  (加练)"
        print(f"\n{mark} {label}{tag}")
        print(f"    {detail}")
        if not ok:
            if required:
                required_failed += 1
            else:
                optional_failed += 1

    total = len(CHECKS)
    passed = total - required_failed - optional_failed
    print("\n" + "-" * 60)
    print(f"通过 {passed}/{total}　必做失败 {required_failed}　加练失败 {optional_failed}")
    if required_failed:
        print("结论：必做项还没全过，接着改 —— 先跑 python loader.py 看它卡在哪。")
        return 1
    if optional_failed:
        print("结论：D1 主体已完成 ✓　加练项想做就补，不想做也能进 D2。")
        return 0
    print("结论：全绿。D1 完成，明天进 D2（把 print 换成 logging）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
