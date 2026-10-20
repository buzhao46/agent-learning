"""Day 2 · asyncio：async/await / gather 并发 / 超时控制

为什么 Agent 开发必须会异步？
    一次巡检要轮 100 台设备，串行 100 × 0.5s = 50s，并发 ≈ 0.5s。
    Agent 里「并行调多个工具」就是靠这个。

运行：
    python day2_async.py                # 纯本地模拟，不联网
    python day2_async.py --real         # 额外用 httpx 真请求 3 个公网地址

验收：看到「串行 vs 并发」耗时对比（并发应明显更快），且超时被正确捕获。
"""
from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any

# 每台设备模拟的响应耗时（秒）
DEVICES = [("core-sw-01", 0.6), ("core-rtr-02", 0.5), ("agg-sw-03", 0.4)]


async def fetch_health(device: str, delay: float) -> dict[str, Any]:
    """模拟一次 SNMP 采集。async def 里的 await 才是真正让出 CPU 的地方。"""
    await asyncio.sleep(delay)  # 真实项目里是 await loop.run_in_executor(...) 包 pysnmp
    if device == "agg-sw-03":
        raise TimeoutError(f"{device} SNMP 无响应")
    return {"device": device, "cpu": 40 + len(device), "delay": delay}


async def sequential() -> tuple[float, list[Any]]:
    """串行：一个 await 完再下一个。"""
    start = time.perf_counter()
    results: list[Any] = []
    for dev, delay in DEVICES:
        try:
            results.append(await fetch_health(dev, delay))
        except TimeoutError as exc:
            results.append({"error": str(exc)})
    return time.perf_counter() - start, results


async def concurrent() -> tuple[float, list[Any]]:
    """并发：gather 一次把所有协程丢进事件循环。

    关键点：return_exceptions=True —— 否则一个失败会整体抛错，
    在巡检场景里「一台设备挂掉不该拖垮整轮巡检」。
    """
    start = time.perf_counter()
    results = await asyncio.gather(
        *(fetch_health(d, t) for d, t in DEVICES), return_exceptions=True
    )
    return time.perf_counter() - start, list(results)


async def with_timeout() -> None:
    """超时控制：wait_for 到点就取消协程，避免卡死整个 Agent。"""
    try:
        await asyncio.wait_for(fetch_health("core-rtr-02", delay=2.0), timeout=0.5)
    except TimeoutError:  # 3.11+ 里 asyncio.TimeoutError 就是内置 TimeoutError
        print("  ✓ wait_for 在 0.5s 触发超时，已取消慢任务")
    else:
        print("  ✗ 没有触发超时，检查 delay/timeout 参数")


async def with_semaphore() -> None:
    """并发限流：一次最多放 2 个请求出去（真实网络设备很怕被打爆）。"""
    sem = asyncio.Semaphore(2)

    async def guarded(dev: str, delay: float) -> str:
        async with sem:
            await asyncio.sleep(delay)
            return dev

    t = time.perf_counter()
    await asyncio.gather(*(guarded(d, 0.3) for d in ["a", "b", "c", "d"]))
    print(f"  4 个任务、并发上限 2 → 耗时 {time.perf_counter() - t:.2f}s（约 0.6s 说明生效）")


async def real_http() -> None:
    """真实异步 HTTP：httpx.AsyncClient + gather（Day 3 之后你会天天用）。"""
    import httpx

    urls = ["https://httpbin.org/delay/1", "https://httpbin.org/delay/1", "https://httpbin.org/delay/1"]
    t = time.perf_counter()
    async with httpx.AsyncClient(timeout=10) as client:
        reqs = [client.get(u) for u in urls]
        resps = await asyncio.gather(*reqs, return_exceptions=True)
    for r in resps:
        # 注意：gather 的异常可能是 BaseException 子类，用 isinstance(r, BaseException) 才能让类型收窄
        print("   ", r if isinstance(r, BaseException) else f"HTTP {r.status_code}")
    print(f"  3 个 1s 接口并发耗时 {time.perf_counter() - t:.2f}s")


async def main() -> None:
    t_seq, seq = await sequential()
    print(f"串行：{t_seq:.2f}s  →  {[r.get('device', r.get('error')) if isinstance(r, dict) else r for r in seq]}")

    t_con, con = await concurrent()
    print(f"并发：{t_con:.2f}s  →  {[r.get('device', str(r)[:20]) if isinstance(r, dict) else str(r)[:24] for r in con]}")
    print(f"提速约 {t_seq / t_con:.1f}x\n")

    print("超时控制：")
    await with_timeout()
    print("并发限流：")
    await with_semaphore()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="额外跑一次真实 HTTP 并发")
    a = ap.parse_args()
    asyncio.run(main())
    if a.real:
        print("真实 HTTP：")
        asyncio.run(real_http())
