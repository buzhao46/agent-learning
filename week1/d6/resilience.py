"""韧性工具：计时 + 重试。

这个文件跟设备、SNMP 都无关，是任何 IO 都能用的通用件。
你今天写的这两个东西，D8 接真 pysnmp、D14 并发采集、M3 部署上线都会原样复用。

一共三个 TODO，都在本文件里：
    ① Timer.__enter__
    ② Timer.__exit__
    ③ retry_call 的函数体

类名、函数签名、参数名都已经定好了，不要改 —— cli.py 和 probe.py 是按这个签名调用的。
你只填函数体。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import TracebackType
from typing import TypeVar

# T 是「随便什么类型」的占位符。retry_call 的返回值类型跟 fn 的返回值类型相同，
# 所以这里用泛型而不是 Any：这样 mypy 能帮你检查
#     n: int = retry_call(read_int)      # ✓
#     s: str = retry_call(read_int)      # ✗ mypy 会报错
T = TypeVar("T")


class Timer:
    """上下文管理器：进 with 时开始计时，离开时算出耗时。

        timer = Timer()
        with timer:
            do_something()
        print(timer.elapsed)      # 秒，float

    为什么用上下文管理器（with）而不是 start()/stop() 两个方法：
    with 块里就算抛异常，退出时也一定会执行 __exit__，不会漏计时；
    而 start()/stop() 一旦中间抛错，stop() 就被跳过了，耗时永远是 0。

    为什么用 perf_counter 而不是 time.time：
    time.time() 是墙上时钟，会被系统对时（NTP）拉回去甚至跳成负数；
    perf_counter() 是单调递增的高精度计数器，专门用来测间隔。
    """

    def __init__(self) -> None:
        self.elapsed: float = 0.0
        self._start: float = 0.0

    def __enter__(self) -> Timer:
        """① 进 with 时会被调用一次。要做两件事：

        - 记下开始时间（存进 self._start）
        - return self

        为什么必须 return self：`with Timer() as t:` 里的 t 拿到的就是 __enter__ 的返回值。
        返回 None 的话，`t` 就是 None，后面 `t.elapsed` 直接 AttributeError。
        """
        self._start = time.perf_counter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """② 离开 with 时一定会被调用，不管有没有抛异常。要做一件事：

        - 算出耗时存进 self.elapsed

        两个关键点：

        1. **不要判断有没有异常**。异常路径也要算耗时——「跑了 8 秒才失败」这个信息
           比「失败」本身有价值得多（说明一直在超时重试，而不是被立刻拒绝）。

        2. **不要 return True**。这个返回值决定异常是否被吞掉：
               return None / False  -> 异常继续往外抛（你要的是这个）
               return True          -> 异常就此消失，调用方永远不知道出过错
           返回类型标的是 -> None，所以直接不写 return 就行。

        三个参数（exc_type / exc / tb）你暂时用不到，但签名里必须有，这是协议规定的。
        """
        self.elapsed = time.perf_counter() - self._start


def retry_call(
    fn: Callable[..., T],
    *args: object,
    attempts: int = 3,
    delay: float = 0.05,
    backoff: float = 2.0,
    **kwargs: object,
) -> T:
    """调用 fn(*args, **kwargs)，失败就重试，最多试 attempts 次。

    attempts   **总尝试次数**（含第一次）。attempts=3 表示最多试 3 次。
               注意：不是「重试 3 次」—— 那样加起来是 4 次。这是本项目最容易写错的地方，
               验收的 C5 和 C7 都在专门测它。
    delay      第一次重试前等多久（秒）
    backoff    每重试一次，等待时间乘几倍。delay=0.05、backoff=2 时：
               0.05s -> 0.1s -> 0.2s -> ...
               这叫「指数退避」。为什么不能固定间隔猛打：
               设备如果是被瞬间打挂的（比如 CPU 100%），你每 50ms 敲一次只会让它更惨，
               越等越久才能给它缓过来的机会。

    返回：fn 成功时的返回值，原样透传。
    失败：attempts 次都抛异常时，抛出**最后一次**的异常（不是第一次的），
          因为最后一次才反映了现在的真实状态。

    实现骨架（三处占位，自己填）：

        last: Exception | None = None          # 记住「最近一次」的异常
        wait = delay
        for i in range(attempts):              # i 是第几次尝试，从 0 开始
            try:
                return fn(*args, **kwargs)     # 成功就直接返回，后面的代码不会执行
            except Exception as exc:           # 【填这里】接住异常
                ...
        # 循环走完说明全失败了
        # 【填这里】把 last 抛出去

    三个必须想清楚的点：

    a) 循环里为什么要用 `for i in range(attempts)` 而不是 `while True`：
       必须有个硬上限。没有上限的重试 = 死循环，线上会直接把进程卡死。

    b) 最后一次失败后还要不要 sleep：
       不要。已经注定要抛异常了，再睡 0.2 秒纯属浪费用户时间。
       所以 sleep 之前要先判断「是不是已经到 attempts 次了」。

    c) 接住异常用 `except Exception`，不要用裸 `except:`：
       裸 except 会把 KeyboardInterrupt（Ctrl+C）和 SystemExit 也一起吞掉，
       程序就变成了「Ctrl+C 杀不死」，只能关窗口。
       （D6 这里为了简单统一重试所有 Exception。更严谨的做法是只重试网络类异常，
         认证失败重试 100 次也没用 —— 见 README 最后一节。）
    """
    last: Exception | None = None
    wait = delay
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last = exc
            if i == attempts - 1:
                break
            time.sleep(wait)
            wait *=backoff
    raise last
