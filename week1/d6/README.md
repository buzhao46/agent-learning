# D6 · 重试与计时

> 21 天训练 第 1 周 / 第 6 天，约 50 分钟
> 原文任务：「用 `retry_call()` 包住采集，用 `Timer` 打印每台设备耗时。技术点：重试、计时。验收：输出里每台设备都有耗时。」

---

## 今天在干什么

前五天你的程序假设「所有设备都能采到、而且瞬时返回」。真实网络里这两条都不成立：

- 设备会偶尔抽风，第一次超时第二次就好了 → 要**重试**
- 有些设备慢得离谱，整体巡检卡 3 分钟 → 要**计时**，才知道时间花在谁身上

今天只加这两件事，但它们是**通用工具**：D8 接真 pysnmp 用同一套，D14 并发采集用同一套，M3 部署上线还在用同一套。

**今天的难点不在代码量，在于三个反直觉的细节**：`attempts` 到底是几次、异常该抛哪一个、`__exit__` 的返回值会吞异常。这三个坑 `check_d6.py` 各有一项专门测。

---

## 目录结构

```
week1/d6/
├── models.py         ← 从 d5 复制（不动）
├── log_setup.py      ← 从 d5 复制（不动）
├── devices.json      ← 已有
├── probe.py          ← 【已写好】采集层，调用你要写的 Timer / retry_call
├── resilience.py     ← 你写（三个 TODO 全在这个文件）
├── cli.py            ← 已复制，改 main 里一处（第 3 步）
├── check_d6.py       ← 验收脚本
└── README.md         ← 这份
```

**关键设计：`probe.py` 调用 `resilience.py`。** 我写的代码依赖你写的代码——你填错，`probe.py` 一跑就崩，不会出现「看起来跑通但其实没接上」。

---

## 第 0 步：确认起点（1 分钟）

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1
.venv\Scripts\activate
cd d6
python check_d6.py
```

应该看到：

```
准备：⚠️  resilience.py 里还有 raise NotImplementedError —— 骨架占位还没换成真正的实现
通过 0/7　必做失败 6　加练失败 1
```

**0/7 是对的**，别慌。骨架里那三个 `raise NotImplementedError` 就是留给你的位置。

> 如果 `.venv\Scripts\activate` 报「禁止运行脚本」，见 D5 的 README 或者改用 `..\..\week1\.venv\Scripts\python.exe check_d6.py`。

---

## 第 1 步：`Timer` —— 上下文管理器（15 分钟）

### 1.1 先搞懂 `with` 到底做了什么

你写：

```python
with Timer() as t:
    do_something()
```

Python 会把它翻译成三步：

```python
_tmp = Timer()              # 1. 造对象
t = _tmp.__enter__()        # 2. 调 __enter__，返回值赋给 as 后面的 t
try:
    do_something()          # 3. 执行 with 块
finally:
    _tmp.__exit__(...)      # 4. 无论如何都调 __exit__
```

**第 4 步是 `finally`，这是整个设计的价值所在。** 对比一下用两个方法写会怎样：

```python
timer.start()
do_something()      # ← 这里抛异常
timer.stop()        # ← 永远不会执行，elapsed 一直是 0
```

而 `with` 保证 `__exit__` 一定跑。所以「计时」这类必须成对出现的操作，用上下文管理器是唯一正确的写法。

你在 Java 里见过对应的东西：`try (Resource r = ...) { }`（try-with-resources）。同一个思路，Python 里靠 `__enter__` / `__exit__` 实现。

### 1.2 填 `__enter__`（第 50 行，函数体在第 59 行）

要做两件事：记开始时间、`return self`。

用 `time.perf_counter()`，**不要用 `time.time()`**：

| | `time.time()` | `time.perf_counter()` |
|---|---|---|
| 是什么 | 墙上时钟（1970 年至今的秒数） | 单调递增的计数器 |
| 会被系统对时影响吗 | **会**。NTP 校时可能让它往回跳，算出负的耗时 | 不会 |
| 精度 | 毫秒级，Windows 上实际约 15.6ms | 亚微秒级 |
| 该用在哪 | 记录「什么时候发生的」 | **测间隔** ← 你要的 |

```python
def __enter__(self) -> Timer:
    self._start = time.perf_counter()
    return self
```

**为什么必须 `return self`**：`as t` 拿到的就是 `__enter__` 的返回值。忘了 return 就是 `None`，后面 `t.elapsed` 立刻 `AttributeError: 'NoneType' object has no attribute 'elapsed'`。

**自检**：

```powershell
python -c "from resilience import Timer; t=Timer(); print(t.__enter__())"
```

看到一个 `<resilience.Timer object at 0x...>` 就对了。如果打印 `None`，说明 return 漏了。

### 1.3 填 `__exit__`（第 61 行，函数体在第 83 行）

只做一件事：算耗时。

```python
def __exit__(self, exc_type, exc, tb) -> None:
    self.elapsed = time.perf_counter() - self._start
```

**两个必须注意的地方：**

**① 不要判断有没有异常。** 别写 `if exc is None: ...`。异常路径也要算耗时——「跑了 8 秒才失败」和「0.1 秒就失败」含义完全不同：前者说明一直在超时重试，后者说明被立刻拒绝（多半是认证错误）。这个信息在排查时非常值钱。

**② 不要 `return True`。** 这个返回值决定异常是否被吞掉：

| `__exit__` 返回 | 效果 |
|---|---|
| `None` / `False` | 异常继续往外抛 ✔ 你要的 |
| `True` | **异常就此消失**，调用方完全不知道出过错 ✘ |

这是今天最隐蔽的坑。写 `return True` 的人通常是「想让程序别崩」，结果是 bug 被彻底掩盖。验收的 C2 专门测这个。

函数签名标的是 `-> None`，所以直接不写 return 就行。

**自检**：

```powershell
python -c "
import time
from resilience import Timer
t = Timer()
with t:
    time.sleep(0.1)
print(f'{t.elapsed*1000:.1f}ms')
"
```

应该打印 `100.xms` 左右。如果打印 `0.0ms`，说明 `__exit__` 里没赋值。

---

## 第 2 步：`retry_call`（20 分钟，今天的核心）

### 2.1 先把 `attempts` 的语义钉死

这是**整个项目最容易写错的地方**，没有之一。

> `attempts = 3` 的意思是：**最多试 3 次**（第 1 次 + 2 次重试）。

不是「重试 3 次」（那样加起来 4 次）。区别在于 `for` 循环怎么写：

```python
for i in range(attempts):        # ✔ 循环 3 次 = 试 3 次
for i in range(attempts + 1):    # ✘ 循环 4 次 = 试 4 次
```

**为什么叫 `attempts` 而不是 `retries`**：`retries` 天生有歧义，有人理解成「额外重试次数」，有人理解成「总次数」。叫 `attempts`（尝试次数）就没有解释空间。你在真实项目里给这类参数起名时，也应该选无歧义的那个。

验收的 C5 会数函数被调用了几次，C7 会用 `--attempts 1/2/3` 分别跑一遍看失败台数。写错方向这两项都会红。

### 2.2 实现

```python
def retry_call(fn, *args, attempts=3, delay=0.05, backoff=2.0, **kwargs) -> T:
    last: Exception | None = None       # 记住「最近一次」的异常
    wait = delay
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)  # 成功立刻返回，后面的代码不执行
        except Exception as exc:
            last = exc                  # ← 注意：是覆盖，不是 `last or exc`
            # 最后一次失败后不要再睡，已经注定要抛异常了
            if i == attempts - 1:
                break
            time.sleep(wait)            # 等一会儿再试
            wait *= backoff             # 下次等更久（指数退避）
    raise last
```

三个关键点，每一个都对应一个坑：

**① `last = exc` 是覆盖，不是 `last = last or exc`**

要用**最后一次**的异常。因为最后一次才反映现在的真实状态：如果第一次是「连接超时」、第三次是「认证失败」，你该看到的是「认证失败」。

写成 `last = last or exc` 会保留第一次的，排查时被误导。D5 里你测过「异常类型要对」，这里是同一个思路的延伸。C5 专门测这个。

**② 最后一次不要 sleep**

已经注定要抛异常了，再睡 0.2 秒纯属浪费用户时间。多台设备各浪费一次，巡检就慢了。

**③ 用 `except Exception`，不要用裸 `except:`**

裸 `except:` 会把 `KeyboardInterrupt`（Ctrl+C）和 `SystemExit` 也吞掉，程序就变成「Ctrl+C 杀不死」，只能关窗口。

`except Exception` 只抓普通异常，`BaseException` 家族的（Ctrl+C、sys.exit）会正常穿透。这个区别在写守护进程、服务端时是致命的。

### 2.3 为什么是「指数退避」而不是固定间隔

`delay=0.05, backoff=2.0` 时，等待时间是 `0.05 → 0.1 → 0.2 → 0.4 ...`。

因为设备挂掉的原因通常是**过载**。你每 50ms 敲一次，只会让它更惨，而且三台设备同时重试等于自己 DDoS 自己。越等越久才是给它缓过来的机会。

真实项目里还会加**抖动（jitter）**：在等待时间上加个随机数，避免几十台设备在同一毫秒一起重试。这个 D14 并发采集时再讲。

**自检**：

```powershell
python -c "
from resilience import retry_call
n = []
def flaky():
    n.append(1)
    if len(n) < 2: raise RuntimeError('第一次失败')
    return 'ok'
print(retry_call(flaky, attempts=3, delay=0.01), '调用次数:', len(n))
"
```

要打印 `ok 调用次数: 2`。

### 2.4 跑一次中间验收

```powershell
python check_d6.py
```

这时应该 **C1–C5 全绿，C6/C7 红**：

```
通过 5/7　必做失败 1　加练失败 1
```

如果 C5 红了说「调用了 4 次」，就是 `range(attempts + 1)` 的 off-by-one。
如果 C5 红了说「里面没有第3次」，就是 `last` 记成了第一次。

---

## 第 3 步：改 `cli.py`（10 分钟）

`resilience.py` 写完了，但 `cli.py` 还在直接打印原始设备数据，根本没调用采集。四处改动：

### ① 加 import（第 15 行 `from models import ...` 下面）

```python
from probe import collect_all
```

### ② 加 `--attempts` 参数（第 56 行下面，`-v` 那行之后）

```python
parser.add_argument("--attempts", type=int, default=3, help="单台设备最多尝试几次（含第一次）")
```

### ③ 加参数校验（第 59 行下面，`--limit` 校验之后）

```python
if args.attempts < 1:
    parser.error(f"--attempts必须是 >= 1 的整数，收到{args.attempts}")
```

照抄 `--limit` 那句的写法就行。`parser.error()` 会打印用法并 `sys.exit(2)`，和 `--limit 0` 的行为一致。

### ④ 把打印换成「先采集再打印」（第 98 行）

**原来**：

```python
print_table(picked)
```

**改成**：

```python
results = collect_all(picked, attempts=args.attempts)
print_table(
    [r.device for r in results],
    {r.device.name: r.elapsed for r in results},
)
```

三行代码在做什么：

- `collect_all(...)` 返回 `list[ProbeResult]`，每个 `ProbeResult` 有 `device` / `elapsed` / `error` 三个字段
- `[r.device for r in results]` —— 列表推导，把结果列表还原成设备列表（**注意：失败的设备对应的是原始数据**，因为没采到新的）
- `{r.device.name: r.elapsed for r in results}` —— 字典推导，造出 `{"core-sw-01": 0.008, ...}` 给 `print_table` 的第二个参数

`print_table` 第二个参数是 `dict[str, float] | None`，给 `None` 就退回 D4 那样不打耗时列。**这种「新功能用可选参数加进来、不破坏老调用」的改法，是重构时最安全的方式。**

**自检**：

```powershell
python cli.py
```

应该看到：

```
设备            IP            状态        CPU  健康        耗时
--------------------------------------------------------------
core-sw-01    10.0.0.1      up         22  ✓      8.2ms
core-rtr-02   10.0.0.2      down       99  ✗     38.6ms
agg-sw-03     10.0.0.3      up         82  ✗     88.5ms

共 3 台，异常 2 台：core-rtr-02, agg-sw-03
```

**注意耗时的规律：8ms → 38ms → 88ms。** 因为第 1 台一次就通，第 2 台失败 1 次，第 3 台失败 2 次。这就是「重试的代价」第一次变得可见。

再看一眼 `--attempts 1`：

```powershell
python cli.py --attempts 1
```

第 2、3 台会失败（`log.error` 打在 stderr），表格里它们显示的是 json 里的原始值。

---

## 第 4 步：跑验收

```powershell
python check_d6.py
```

目标 **7/7**。红了就照下面查：

| 报错 | 原因 |
|---|---|
| 准备检查：`还有 raise NotImplementedError` | 三个 TODO 没填完，或者填了但把 `raise` 那行留在上面 |
| C1：`elapsed=0.0` | `__exit__` 里没赋值，或者用 `time.time()` 而没减对 |
| C1：`elapsed` 是 60 而不是 0.06 | 单位搞错，用了毫秒 |
| C2：`ValueError 没被接住` | `__exit__` 返回了 `True`，异常被吞了 |
| C2：`异常路径上 elapsed=0.0` | `__exit__` 里加了 `if exc is None` 判断，只在成功时算 |
| C3：`首次成功却调用了 2 次以上` | 循环结构写成了「先试一次，失败后再 for 循环 attempts 次」，加起来多了一次 |
| C4：`调用 2 次但返回 None` | 成功后没 `return fn(...)`，只是没抛异常 |
| C5：`调用了 4 次` | `range(attempts + 1)` 的 off-by-one |
| C5：`里面没有第3次` | `last = last or exc` 记成了第一次 |
| C5：`抛出的异常类型变了` | `raise last` 写成了 `raise SomeOtherError(...)` |
| C6：`没有耗时列` | `cli.py` 第 98 行还没改成 `print_table(..., elapsed)` |
| C6：`耗时算出来是 0.0ms` | `collect_all` 的返回值没取对，或者 Timer 没算 |
| C7：`还不认识 --attempts` | 参数没加，或者拼成了 `--attempt` |
| C7：`attempts=1 时只有 1 台失败` | 你把 attempts 当成「额外重试次数」了 |

---

## 第 5 步：收尾（5 分钟）

### 1. 顺手删掉 `cli.py` 里那段任务说明注释

第 86–97 行那段 `# 【你要改的就是这里】...` 是今天的施工图，活儿干完了就该拆掉。删到只留 `print_table` 那句。

### 2. 看一眼 `-v`

```powershell
python cli.py -v
```

应该能看到每台设备「采集完成 xx.xms」的 DEBUG 行——那是 `probe.py` 里的 `log.debug`。**表格给人看（stdout），耗时明细给排查看（stderr 日志）**，这个分工要记住。

### 3. 提交

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35
git add .
git status
git commit -m "D6: 加 Timer 与 retry_call，巡检输出带耗时"
git pushall
```

`git status` 要确认加进去的是 `week1/d6/` 下的文件，**没有 `__pycache__` 和 `.venv`**。

---

## 三个坑，记住它们

### 坑一：`attempts + 1`

新手写重试，10 个里有 6 个会多试一次。原因是想当然地认为「重试 3 次 = 再试 3 次」。

解法不是记住哪个对，而是**给参数起无歧义的名字**（`attempts` 而非 `retries`），然后在测试里断言调用次数。你今天写的 C5 就是干这个的。

### 坑二：`__exit__` 返回 `True`

看起来像是「让程序别崩」的好心，实际上是**把错误彻底隐藏**。

判据：**`__exit__` 返回 `True` 只在一种情况下是对的——你明确要吞掉这个异常，并且在这里做了替代处理（比如记日志、返回降级结果）。** 否则一律返回 `None`。

### 坑三：记住第一次的异常

`last = last or exc` 写起来很顺手，但它保留的是**最早的**错误。而重试场景里，最早的错误往往已经不再成立（第一次可能只是抖动），最新那次才是真相。

---

## 延伸：什么样的错误**不该**重试

今天为了简单，`except Exception` 会重试所有异常。但真实项目里这是错的，而且会造成严重后果：

| 错误类型 | 该重试吗 | 为什么 |
|---|---|---|
| 连接超时 / 网络不可达 | ✔ 该 | 抖动的可能性大，等一会儿多半就好了 |
| 设备忙（SNMP 返回 genErr） | ✔ 该 | 过载是暂时的 |
| **认证失败（community 错）** | ✘ **不该** | 重试 100 次也是错。3 次重试 × 20 台设备 = 白等 60 次超时 |
| **OID 不存在** | ✘ 不该 | 配置错误，得改配置 |
| **配置文件格式错** | ✘ 不该 | 同上 |

**认错失败最典型**：把 community 写错之后，巡检会卡住好几分钟，最后报一堆「超时」——真正的错误信息（认证失败）被埋在噪声里。这是运维工具最常见的烂体验。

正确做法是定义一个基类，只重试它：

```python
class RetryableError(RuntimeError):
    """网络抖动类，可以重试"""

class ProbeTimeout(RetryableError):
    pass

class AuthError(RuntimeError):
    """认证失败，重试无意义，不继承 RetryableError"""
    pass
```

然后 `retry_call(..., retry_on=RetryableError)` 只捕获那个类型。

**这个改造排在 D12**（原文：错误分类——超时/认证失败/无此 OID 三种情况给三种提示）。今天先知道有这么回事，别改。

---

## 明天：D7 合成 CLI

D7 是把前 6 天合成 `snmpcheck`，带 `list` / `status` 两个子命令（`subparsers`）。那是 21 天训练第 1 周的最后一天，做完这个工具就能写进简历了。

---

## 卡住了怎么办

1. 先看第 4 步的对照表，八成有对应条目
2. 单跑某一项不方便，就把 `check_d6.py` 里的 `CHECKS` 只留那一行
3. 还是不行，把 `python check_d6.py` 的完整输出贴给我
