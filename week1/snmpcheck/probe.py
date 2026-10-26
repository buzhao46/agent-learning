"""采集层：从设备读回最新状态。

D6 阶段是「模拟采集」——故意让某些设备先失败几次，用来练重试。
D8 会把 `make_simulated_probe` 整个换成真的 pysnmp getCmd，其余代码不用动。

这个文件是**已经写好的**，你不需要改。它调用 resilience.py 里的 Timer 和 retry_call，
那两个才是你今天要写的。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from log_setup import get_log
from models import Device
from resilience import Timer, retry_call


class ProbeError(RuntimeError):
    """采集失败：超时、连不上、返回的数值不合理，都归到这里。

    独立于 models.SnmpError：SnmpError 说的是「配置有问题」（改配置才能好），
    ProbeError 说的是「这台设备这次没采到」（重试可能就好了）。两者的处理策略不同。
    """


def make_simulated_probe(device: Device, *, fail_times: int = 0) -> Callable[[], Device]:
    """造一个「模拟采集函数」，返回零参 callable，可以反复调用。

    为什么用闭包而不是直接写 probe_device(device)：
    retry_call 会**多次调用同一个函数**，而「已经失败过几次」这个状态必须留住。
    如果放在模块级全局变量里，两次 collect 之间会互相污染，测试也没法隔离。

    fail_times   这台设备前几次调用要失败（模拟网络抖动）
    返回          一个新的 Device：cpu 用设备名确定性算出，status 按 cpu 推断
    """
    left = {"n": fail_times}

    def _probe() -> Device:
        # 一次真实 SNMP 往返至少是毫秒级（局域网 2~10ms，跨机房几十 ms）。
        # 不加这个 sleep，本机跑出来的耗时会小到 0.04ms，表格里只剩 0.0ms，看不出任何东西。
        time.sleep(0.008)
        if left["n"] > 0:
            left["n"] -= 1
            raise ProbeError(f"{device.name} 采集超时")

        cpu = (sum(ord(c) for c in device.name) * 7) % 100
        status = "down" if cpu >= 95 else "up"
        return replace(device, cpu=cpu, status=status)

    return _probe


@dataclass
class ProbeResult:
    """一台设备的采集结论。ok 为 False 时 device 是原始数据（没采到新的）。"""

    device: Device
    elapsed: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def collect_all(devices: list[Device], *, attempts: int = 3) -> list[ProbeResult]:
    """逐台采集，每台都计时 + 重试。

    attempts  单台设备的**总尝试次数**（含第一次）。attempts=3 表示最多试 3 次。
              注意不是「重试 3 次」——那样总共会试 4 次，是最常见的 off-by-one 误解。

    为什么失败的那台也带着 elapsed 返回：
    巡检报告里「这台耗时 8 秒最后还是失败」比「这台失败」有用得多，
    因为 8 秒意味着它一直在超时重试，而 0.1 秒就失败说明是立刻被拒绝（比如认证错误）。
    """
    log = get_log()
    results: list[ProbeResult] = []

    for index, device in enumerate(devices):
        # 第 0 台一次就通，第 1 台抖一下，第 2 台抖两下——固定的实验装置，
        # 这样每次跑结果都一样，验收才能稳定复现。真实世界的抖动是随机的。
        probe = make_simulated_probe(device, fail_times=index % 3)

        timer = Timer()
        try:
            with timer:
                fresh = retry_call(probe, attempts=attempts, delay=0.02)
        except ProbeError as exc:
            results.append(ProbeResult(device=device, elapsed=timer.elapsed, error=str(exc)))
            log.error("设备 %s 采集失败（耗时 %.1fms）：%s", device.name, timer.elapsed * 1000, exc)
            continue

        results.append(ProbeResult(device=fresh, elapsed=timer.elapsed))
        log.debug("设备 %s 采集完成 %.1fms", fresh.name, timer.elapsed * 1000)

    return results
