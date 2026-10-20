"""Day 1 · Python 基础速通：类型注解 / dataclass / 异常 / 文件 / 日志

你有 Java 底子，这一天重点看「Python 怎么表达 Java 里那些概念」：
    Java class + getter      -> @dataclass
    Java interface           -> Protocol / TypedDict（后面会用到）
    Java checked exception   -> 自定义异常（Python 不强制 catch）
    Java Optional<T>         -> T | None
    Maven/venv               -> venv + pip + requirements.txt

运行：
    python day1_types.py
    python day1_types.py --device core-sw-01
    python day1_types.py --device not-exist

验收：不报错跑完；out/day1_devices.json 生成；未知设备时打印友好错误而不是崩栈。
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")
log = logging.getLogger("day1")

# Literal = 只能是这几个值，等价于 Java 的 enum（但零成本，运行时就是 str）
Status = Literal["up", "down"]


class SnmpError(RuntimeError):
    """自定义异常。业务代码里 raise SnmpError("...")，调用方按需捕获。"""


@dataclass
class Device:
    """用 dataclass 描述一台设备，自动生成 __init__ / __repr__ / __eq__。"""

    name: str
    ip: str
    status: Status = "up"
    cpu: int = 0
    tags: list[str] = field(default_factory=list)  # 坑：可变默认值必须用 default_factory

    def is_healthy(self, cpu_limit: int = 80) -> bool:
        return self.status == "up" and self.cpu < cpu_limit

    def to_dict(self) -> dict[str, object]:
        return asdict(self)  # 转 dict，方便写 JSON


# 用假的「数据库」代替真实采集，Day 5 会换成 pysnmp
SEED: list[Device] = [
    Device("core-sw-01", "10.0.0.1", "up", 92, ["core", "switch"]),
    Device("core-rtr-02", "10.0.0.2", "up", 41, ["core", "router"]),
    Device("agg-sw-03", "10.0.0.3", "down", -1, ["agg", "switch"]),
]


def find_device(devices: list[Device], name: str) -> Device:
    """找不到就抛异常，而不是返回 None —— 让调用方决定怎么处理。"""
    for d in devices:
        if d.name == name:
            return d
    raise SnmpError(f"设备不存在：{name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Day 1 演示")
    parser.add_argument("--device", help="只看某台设备，例如 core-sw-01")
    parser.add_argument("--out", default="out/day1_devices.json", help="输出文件路径")
    args = parser.parse_args()

    try:
        if args.device:
            picked = [find_device(SEED, args.device)]
        else:
            picked = SEED
    except SnmpError as exc:  # 只捕获「预期内」的异常，别写裸 except
        log.error("%s", exc)
        return 2

    # ---- 写文件：pathlib 比 os.path 好用，mkdir(parents=True) 会自动建父目录
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"count": len(picked), "devices": [d.to_dict() for d in picked]}
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已写入 %s", out_path)

    # ---- 打印汇总（f-string 里用 :<20 做左对齐）
    print(f"\n{'设备':<14}{'IP':<14}{'状态':<8}{'CPU':>5}  健康")
    print("-" * 52)
    for d in picked:
        print(f"{d.name:<14}{d.ip:<14}{d.status:<8}{d.cpu:>5}  {'✓' if d.is_healthy() else '✗'}")

    unhealthy = [d.name for d in SEED if not d.is_healthy()]
    print(f"\n共 {len(SEED)} 台，异常 {len(unhealthy)} 台：{', '.join(unhealthy) or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
