"""Day 1 加练：类型注解钻练（目标 = 让 mypy 零报错）

这个文件里有 3 处**故意写错**的类型。你要做的是：
    1) 先跑：  mypy day1_typing_drill.py
    2) 读懂每一行报错在说什么
    3) 用「改代码」或「改注解」的方式修掉它 —— 两种都算对，但要能说出为什么选这个
    4) 修完再跑一次，看到 Success 才算过

为什么值得练：类型注解在 Agent 项目里不是审美问题。
    - Function Calling 的 parameters schema 本质就是类型声明，写错模型就传错参数
    - Pydantic 模型靠注解做校验，注解写松了脏数据就进系统
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["up", "down"]


@dataclass
class Device:
    name: str
    cpu: int
    status: Status = "up"


# ---- 错误 1：返回值类型对不上
# 预期报错：Incompatible return value type (got "str", expected "int")
def total_cpu(devices: list[Device]) -> int:
    if not devices:
        return "0"  # 这里错了：想表达「没有设备时返回 0」，但返回了字符串


# ---- 错误 2：字面量类型写错值
# 预期报错：Argument "status" ... has incompatible type "str"; expected "Literal['up', 'down']"
def make_device(name: str, status: str) -> Device:
    return Device(name=name, cpu=0, status=status)


# ---- 错误 3：可能为 None 的值直接用了
# 预期报错：Item "None" of "Device | None" has no attribute "cpu"
def find(devices: list[Device], name: str) -> Device | None:
    return next((d for d in devices if d.name == name), None)


def cpu_of(devices: list[Device], name: str) -> int:
    dev = find(devices, name)
    return dev.cpu  # 如果查不到，这里会 AttributeError —— mypy 提前帮你发现


if __name__ == "__main__":
    demo = [Device("core-sw-01", 92), Device("core-rtr-02", 41)]
    print("总 CPU：", total_cpu(demo))
    print("查 core-sw-01：", cpu_of(demo, "core-sw-01"))
    print("查不存在：", cpu_of(demo, "nope"))  # 修好错误 3 之后，这里应该给出友好提示
