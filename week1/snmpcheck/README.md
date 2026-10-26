# D7 · 合成 snmpcheck

> 21 天训练 第 1 周 / 第 7 天（收官），约 60 分钟
> 原文任务：「把前 6 天合成 `snmpcheck`，带 `list` / `status` 两个子命令。技术点：subparsers。验收：`snmpcheck list` 与 `status` 都能跑。」

---

## 先说清楚三件事

### 一、目录叫 `snmpcheck/`，不叫 `d7/`

前六天是 `d1/` … `d6/`，每天一个新目录，因为每天是独立练习。

但**今天是产品成型**，不是练习。所以：

```
week1/snmpcheck/        ← 你的项目主目录，从现在起
week1/d1 … d6/          ← 保留不动，当历史存档
```

**D8 到 D21 都在 `snmpcheck/` 里改，不再新建 `d8/`、`d9/`。** 原因很实在：如果每天复制一份，到 D21 你会有 14 份近似副本，改一个 bug 要改 14 处，而且根本讲不清「哪份是最终的」。

同一个目录被改 15 次，你对每一行都清楚——这才是面试里能讲深的前提。

### 二、为什么要两个子命令，而不是一堆参数

你现在的 `cli.py` 是这样：

```powershell
python cli.py --file x.json --limit 2        # 想干什么？看名字看不出来
python cli.py --attempts 1                   # 这是采集还是只列清单？不知道
```

加个 `--no-collect` 之类的 flag 也能凑合，但会越来越乱。子命令把两类操作分开：

```powershell
python snmpcheck.py list      # 只读配置，不碰设备。快，用来确认配置对了
python snmpcheck.py status    # 真去采集，慢，可能失败
```

**判据：如果一个命令有两种「意图完全不同」的用法，就该拆成子命令。** 你熟悉的 `git status` / `git log` / `git push` 也是这个设计。

### 三、今天不写新业务逻辑

`cmd_list` / `cmd_status` 里的代码，**八成是从 D6 的 `main` 里搬过来的**。今天真正的新东西只有：`subparsers` + 命令分发。

---

## 目录结构

```
week1/snmpcheck/
├── models.py        ← 从 d6 复制（不动）
├── log_setup.py     ← 从 d6 复制（不动）
├── probe.py         ← 从 d6 复制（不动）
├── resilience.py    ← 从 d6 复制（不动，你写的 Timer/retry_call）
├── devices.json     ← 从 d6 复制
├── snmpcheck.py     ← 今天的工作台（原来叫 cli.py）
├── check_d7.py      ← 验收脚本
└── README.md        ← 这份（D17 会重写成对外的产品文档）
```

`snmpcheck.py` 里**已经写好**的部分：

| 行号 | 内容 | 说明 |
|---|---|---|
| 35 | `print_table` | 从 D6 搬来，一字未改 |
| 65 | `positive_int` | 新加的，见下面「顺手学的一招」 |
| 82 | `load_and_filter` | 新加的，`list` 和 `status` 共用的筛选逻辑 |

**你要写的四个**（都在文件后半段）：

| 行号 | 函数 | 大约行数 |
|---|---|---|
| 113 | `cmd_list` | 5 行 |
| 126 | `cmd_status` | 10 行 |
| 141 | `build_parser` | 20 行 ← 今天的主角 |
| 189 | `main` | 5 行 |

### 顺手学的一招：`positive_int`

D3–D6 你都是这么校验的：

```python
if args.limit is not None and args.limit < 1:
    parser.error(f"--limit必须是 >= 1 的整数，收到{args.limit}")
```

今天换了个写法，第 65 行：

```python
def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"必须是 >= 1 的整数，收到 {value}")
    return value
```

然后在 `add_argument` 里写 `type=positive_int`。好处：

1. **校验跟着参数定义走**，不会再出现「加了个新参数忘了校验」
2. 错误信息自动带上参数名，不用手写 `--limit` 那四个字
3. 校验逻辑只写一次，`--limit` 和 `--attempts` 共用

`type=` 接受任何「字符串进、值出」的函数。`type=int` 其实也是这种函数，只不过它不做范围检查。

---

## 第 0 步：确认起点（1 分钟）

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1
.venv\Scripts\activate
cd snmpcheck
python check_d7.py
```

应该看到：

```
准备：⚠️  snmpcheck.py 里还有 raise NotImplementedError —— 四个函数还没写完
通过 0/7　必做失败 6　加练失败 1
```

**0/7 是对的。** 四个 `raise NotImplementedError` 就是留给你的位置。

---

## 第 1 步：`build_parser` —— 今天的主角（25 分钟）

### 1.1 先理解 subparsers 在做什么

不用 subparsers 的话，你得这么写：

```python
if args.command == "list":
    ...
elif args.command == "status":
    ...
```

能用，但每加一个子命令就要改 `main`，而且所有参数都挤在一个 parser 里——`list` 也会接受 `--attempts`，语义就乱了。

subparsers 做的事：**在解析阶段就把两个子命令隔离开，各自有独立的参数集**。`list --attempts` 会直接报「不认识这个参数」，因为 `list` 这个 parser 里压根没定义它。

这就是为什么 `list` 没有耗时列、也不需要 `--attempts`——它的参数集里根本没有这些东西。

### 1.2 三行核心

第 141 行的 `build_parser` 里有三段骨架，照着填：

```python
parser = argparse.ArgumentParser(prog=PROG, description="设备巡检工具：先确认配置，再采一遍")
sub = parser.add_subparsers(dest="command", required=True)      # ①

p_list = sub.add_parser("list", help="只列设备清单，不采集")      # ②
p_list.add_argument("--file", default=str(DEFAULT_FILE), help="设备清单 json 路径")
p_list.add_argument("--device", help="只看某台设备，例如 core-sw-01")
p_list.add_argument("--limit", type=positive_int, help="只输出前N台设备")
p_list.set_defaults(func=cmd_list)                              # ③

p_status = sub.add_parser("status", help="采集每台设备并输出状态与耗时")
p_status.add_argument("--file", default=str(DEFAULT_FILE), help="设备清单 json 路径")
p_status.add_argument("--device", help="只看某台设备，例如 core-sw-01")
p_status.add_argument("--limit", type=positive_int, help="只采集前N台设备")
p_status.add_argument("--attempts", type=positive_int, default=3, help="单台最多尝试几次（含第一次）")
p_status.add_argument("-v", "--verbose", action="store_true", help="打开DEBUG日志")
p_status.set_defaults(func=cmd_status)

return parser
```

三行标号分别对应三个必须理解的点：

### 1.3 ① `required=True` 为什么必须有

去掉它试试：`python snmpcheck.py` 什么都不做，退出码 **0**。

用户敲漏了一个词，程序静默成功——这是最糟糕的体验。加上之后：

```
usage: snmpcheck [-h] {list,status} ...
snmpcheck: error: the following arguments are required: command
```

退出码 2。**退出码很重要**：你在 shell 脚本、CI、cron 里判断成功失败，靠的就是它。C3 专门测这个。

> 小历史：Python 3.7 之前不能直接传 `required=True`，得写 `sub.required = True` 单独赋值。你用的是 3.13，直接传参就行。

### 1.4 ③ `set_defaults(func=...)` 是命令模式的精髓

`set_defaults` 把 `func=cmd_list` 塞进 `args` 的默认值里。于是 `main` 里只要一行：

```python
return args.func(args)
```

**不用写 if/elif 一大串。** 而且以后加第三个子命令（比如 D11 之后可能要 `--output csv`），你只需要在 `build_parser` 里加两行，`main` 一行都不用动。

这个套路叫**命令模式**（Command Pattern）：把「要做什么」变成一个可传递的值，而不是一堆分支判断。你在 Go 命令行、git 子命令、各种 CLI 框架里都会看到它。

### 1.5 参数归属：加在哪个 parser 上，就只能写在哪

这是 subparsers 最容易懵的地方。

| 定义位置 | 用法 | `list --file x` 能用吗 |
|---|---|---|
| 子命令 `p_list` 上（今天用这个） | `snmpcheck list --file x` | ✔ |
| 顶层 `parser` 上 | `snmpcheck --file x list` | ✘ 报错 |

两种都能工作，但**不能混**。混了就会出现「有的参数写在命令前、有的写在命令后」这种让人记不住的界面。

今天统一加在**子命令**上，因为 `snmpcheck list --file x` 符合直觉——参数跟着命令走。

**自检（第 1 步做完但还没写 main 时做不了，跳到第 2 步一起验）**

---

## 第 2 步：`main` —— 分发（10 分钟）

第 189 行：

```python
parser = build_parser()
args = parser.parse_args()
level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
get_log(level)
return args.func(args)
```

### 2.1 这里有个坑：`args.verbose` 可能不存在

`status` 定义了 `-v`，`list` **没有**。所以：

```
args = Namespace(command='list', file='...', device=None, limit=None, func=cmd_list)
                                                                  ↑ 没有 verbose
```

直接写 `args.verbose` → `AttributeError: 'Namespace' object has no attribute 'verbose'`。

有四种解法，你选一个：

| 方法 | 写法 | 评价 |
|---|---|---|
| `getattr` 带默认值 | `getattr(args, "verbose", False)` | ✔ 最短，推荐 |
| `hasattr` 判断 | `if hasattr(args, "verbose") and args.verbose` | 能用，啰嗦 |
| `vars()` 里查 | `args.__dict__.get("verbose", False)` | 能跑，但别这么写 |
| 给两个子命令都设默认 | `p_list.set_defaults(func=cmd_list, verbose=False)` | ✔ 也干净，但要记得两边都写 |

**核心认知**：`Namespace` 不是一个有固定字段的类，它就是个**装着「这个 parser 定义了什么」的字典**。所以永远不能假设某个属性一定存在。

D3 那个 `args.v` / `args.verbose` 的报错，本质是同一件事——**你访问的名字和实际存进去的名字对不上**。这次是名字对但属性压根没被创建。

### 2.2 顺序：必须先建 logger，再分发

```python
get_log(level)        # ← 必须在前
return args.func(args)  # ← 必须在后
```

因为 `load_and_filter` 和 `collect_all` 内部都会调 `get_log()`。logger 没初始化过的话，`logging.getLogger("snmpcheck")` 返回的是个**默认配置的 logger**——级别是 `WARNING`，你的 `log.info(...)` 全被吞掉，一行都看不到。

**这类问题最难查**，因为程序不报错，就是「日志没出来」。判据：**任何用到日志的代码路径，执行前必须先有 `setup`。**

### 2.3 自检

```powershell
python snmpcheck.py --help
```

应该看到：

```
usage: snmpcheck [-h] {list,status} ...

设备巡检工具：先确认配置，再采一遍

positional arguments:
  {list,status}
    list         只列设备清单，不采集
    status       采集每台设备并输出状态与耗时
```

再试：

```powershell
python snmpcheck.py
```

应该报 `error: the following arguments are required: command`，退出码 2。

检查退出码：

```powershell
python snmpcheck.py; echo "退出码: $LASTEXITCODE"
```

**做到这里，C3 和 C4 应该绿了。** 但 `list` / `status` 还会因为 `cmd_*` 是 NotImplementedError 而崩——正常，下一步解决。

---

## 第 3 步：`cmd_list`（5 分钟）

第 113 行。基本是「调两个函数」：

```python
picked = load_and_filter(args)
if picked is None:
    return 2
print_table(picked)
return 0
```

**关键：`print_table(picked)` 只传一个参数。** 就是这一处决定了 list 没有耗时列——因为 `elapsed` 是 `None`，`print_table` 里那段加耗时列的逻辑就走不到。

C1 专门验证这个：它检查 list 的输出里**不能**出现 `数字.ms`。如果你不小心把 `cmd_status` 的实现复制过来了，C1 立刻红。

**自检**：

```powershell
python snmpcheck.py list
```

应该看到 3 台设备，**没有耗时列**，而且 CPU 显示的是 json 里的原始值（92 / 41 / -1），因为没采集：

```
设备            IP            状态        CPU  健康
----------------------------------------------------
core-sw-01    10.0.0.1      up         92  ✗
core-rtr-02   10.0.0.2      up         41  ✓
agg-sw-03     10.0.0.3      down       -1  ✗
```

再试 `python snmpcheck.py list --limit 2`，应该只出 2 台。

---

## 第 4 步：`cmd_status`（10 分钟）

第 126 行。这段几乎是把 D6 的 `main` 后半截搬过来：

```python
log = get_log()
picked = load_and_filter(args)
if picked is None:
    return 2

results = collect_all(picked, attempts=args.attempts)
print_table(
    [r.device for r in results],
    {r.device.name: r.elapsed for r in results},
)
log.info("巡检结束，共输出 %d 台", len(picked))
return 0
```

搬的时候注意两点：

1. **`args.attempts` 是 `status` 独有的**，`list` 里没有。所以这段只能放在 `cmd_status`，不能放到 `load_and_filter` 里
2. `get_log()` 在这里调是**拿**已经建好的 logger，不是新建——因为 `main` 已经调过 `get_log(level)` 了（这就是第 2 步强调顺序的原因）

**自检**：

```powershell
python snmpcheck.py status
```

应该看到**带耗时列**，且 CPU 是采集后的新值（22 / 99 / 82）：

```
设备            IP            状态        CPU  健康        耗时
--------------------------------------------------------------
core-sw-01    10.0.0.1      up         22  ✓      8.4ms
core-rtr-02   10.0.0.2      down       99  ✗     38.9ms
agg-sw-03     10.0.0.3      up         82  ✗     92.0ms
```

**注意 list 和 status 的输出差异**：同一个 `print_table`，同一个设备清单，出来的东西完全不同。这就是「子命令把两种意图分开」的价值。

再试一个：

```powershell
python snmpcheck.py status --attempts 1
```

第 2、3 台会失败（日志里是 ERROR），因为 `--attempts 1` 意味着只试一次，而 1 号设备（core-rtr-02）需要试 2 次、2 号（agg-sw-03）需要 3 次。

---

## 第 5 步：跑验收

```powershell
python check_d7.py
```

目标 **7/7**。红了照下面查：

| 报错 | 原因 |
|---|---|
| 准备检查：还有 `raise NotImplementedError` | 四个函数没写完 |
| C3：`退出码是 0` | `add_subparsers` 少了 `required=True` |
| C4：`看不到 list/status` | `sub.add_parser("list", ...)` 那两行没写，或者写成了 `parser.add_parser` |
| C4：`没有 {list,status}` | 子命令加到别的 parser 上了 |
| C1：`AttributeError: verbose` | `main` 里直接写了 `args.verbose`，要用 `getattr(args, "verbose", False)` |
| C1：`带了耗时` | `cmd_list` 里 `print_table` 传了两个参数 |
| C1：`应列出 3 台，实际 0 行` | `cmd_list` 没调 `print_table`，或者没调 `load_and_filter` |
| C2：`缺少耗时列` | `cmd_status` 里 `print_table` 只传了一个参数 |
| C5：`list --limit 2 输出 3 台` | `--limit` 加在了错误的 parser 上，或者 `load_and_filter` 没被调用 |
| C6：`unrecognized arguments: --attempts` | `--attempts` 加到顶层 parser 上了。要么改到 `p_status`，要么写 `snmpcheck.py --attempts 1 status` |
| C6：`应有 2 台失败，实际 0` | `collect_all` 没接到 `args.attempts` |
| C7：`python -m snmpcheck 失败` | 用了 `from .models import ...` 这种带点的相对导入。D7 阶段全部用不带点的绝对导入 |

---

## 第 6 步：收尾

### 1. 清理 `build_parser` 里那段骨架说明

第 141 行的 docstring 里有一大段带编号的说明（a/b/c 三条 + 参数归属那段），活儿干完了就该删到只剩一句：

```python
def build_parser() -> argparse.ArgumentParser:
    """搭出带 list / status 两个子命令的解析器。"""
```

`cmd_list` / `cmd_status` / `main` 里的「要求：」段落同理。

**为什么值得花两分钟删**：一周后你回来看这段代码，那些「要求」会让你怀疑自己是不是没做完。**注释描述的是「当时要做什么」，代码实现完之后，它就从帮助变成了干扰。**

### 2. 提交

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35
git add .
git status
git commit -m "D7: 合成 snmpcheck，带 list/status 子命令"
git pushall
```

`git status` 要确认看到的是 `week1/snmpcheck/` 整个目录（7 个文件），**没有 `__pycache__`、没有 `out/`**。

> `out/` 里是运行日志。仓库根的 `.gitignore` 已经排除了 `out/` 和 `__pycache__/`，所以 `git status` 里**不该**看到它们。
>
> 如果看到了，说明 `.gitignore` 没生效——最常见的原因是这些文件**之前已经被 `git add` 过**。`.gitignore` 只对「还没被跟踪」的文件有效，对已跟踪的文件完全无效。真要补救得用 `git rm --cached 路径`，把这个文件从索引里摘掉但保留磁盘上的文件。

---

## 三个坑

### 坑一：`Namespace` 里不一定有你要的属性

`args.verbose` 在 `status` 下存在、在 `list` 下不存在。这是 subparsers 的常态，因为每个子命令有自己的参数集。

**通用解法**：`getattr(args, "名字", 默认值)`。这个习惯要养成，不只是 argparse——任何「可能没有这个属性」的地方都适用。

### 坑二：参数加在哪个 parser 上，决定了它必须写在哪

```powershell
snmpcheck --file x.json list     # 顶层参数，必须在子命令前
snmpcheck list --file x.json     # 子命令参数，必须在子命令后
```

两者不能混。今天的约定是**全部加在子命令上**。

如果你想「两种位置都支持」，正确做法是给顶层 parser 和子命令**都加一遍**（用同一个 `parent` parser，见下面延伸）。但绝大多数场景不需要——挑一种，保持一致就好。

### 坑三：`set_defaults` 忘了写

`p_list.set_defaults(func=cmd_list)` 漏了，`main` 里 `args.func` 就 AttributeError。

而且这个错误只在**运行到那一刻**才出现，`--help` 完全正常（因为 help 不执行 func）。所以「--help 看着挺好」不代表子命令能用。

---

## 延伸一：想让参数不重复写两遍

现在 `--file` / `--device` / `--limit` 在 `p_list` 和 `p_status` 里各写了一遍。三个参数 × 2 = 六行重复。

标准解法是 **parent parser**：

```python
common = argparse.ArgumentParser(add_help=False)
common.add_argument("--file", default=str(DEFAULT_FILE), help="设备清单 json 路径")
common.add_argument("--device", help="只看某台设备")
common.add_argument("--limit", type=positive_int, help="只处理前N台")

p_list = sub.add_parser("list", parents=[common], help="...")
p_status = sub.add_parser("status", parents=[common], help="...")
```

`add_help=False` 是必须的——否则父 parser 会带一个 `-h`，和子命令自己的 `-h` 冲突，报 `conflicting option string`。

**今天不用改**，因为只重复了 6 行，改反而增加理解成本。等 D11 之后参数涨到 6–7 个再动手。**重复 2 次可以忍，重复 3 次才该抽。**（这条规则叫「rule of three」）

---

## 延伸二：D19 之后就不用敲 `python snmpcheck.py` 了

现在的用法：

```powershell
python snmpcheck.py list
```

D19 会写 `pyproject.toml` + `entry_points`，然后 `pip install -e .`，之后在**任何目录**都能：

```powershell
snmpcheck list
```

原理：`pip install` 会在虚拟环境的 `Scripts/` 下生成一个 `snmpcheck.exe` 包装器，里面就一句「找到 `snmpcheck:main` 并调用」。所以入口函数必须叫 `main` 且返回退出码——你今天写的 `main` 正好满足。

这也是为什么 D7 的验收标准原文写的是 `snmpcheck list`——它指的是最终的形态。今天先按 `python snmpcheck.py list` 跑，D19 再摘掉前缀。

---

## 第 1 周结束：你现在手上有什么

七天下来，`week1/snmpcheck/` 里有这些东西（全是你自己写的）：

| 文件 | 提供的能力 | 对应哪天 |
|---|---|---|
| `models.py` | `Device` 数据类、json 读写、异常 | D1 + D4 |
| `log_setup.py` | 分级日志、stderr + 文件双输出 | D2 + D4 |
| `resilience.py` | `Timer` 上下文管理器、`retry_call` 指数退避 | D6 |
| `probe.py` | 采集层（今天是模拟的） | D6 |
| `snmpcheck.py` | `list` / `status` 两个子命令 | D3 + D5 + D7 |
| `d5/test_device.py` | 8 个 pytest 用例 | D5 |

**这件事的分量，可能比你感觉的大。** 一周前你说「Python 只是会基础，不常写脚本」；现在的状态是：

- 能从零写出带类型注解的 dataclass，知道 `cast` / `Literal` / `field(default_factory=...)` 各解决什么问题
- 知道日志该怎么分层（过程信息进日志、最终产物走 print），也知道 handler 守卫和 `encoding="utf-8"`
- 知道怎么把一个 250 行的文件拆成四层（数据 / 基础设施 / 采集 / 入口），并且依赖只能单向
- 知道 pytest 怎么写「真能抓 bug」的测试——因为你的测试被四种变异验证过
- 知道上下文管理器协议、指数退避、命令模式、参数作用域

**这些不是「看过」，是「写过、改过、被验收打过脸」。** 两者的差别在面试里一问就分得出来。

还没做到的：`probe.py` 里的采集是**模拟**的。它现在返回的 `cpu = (sum(ord(c) for c in name) * 7) % 100`，是个假数据。

**D8 就是把它换成真的 pysnmp。** 那时 `resilience.py` 一个字不用改——因为你写的 `retry_call` 和采集逻辑完全解耦了。这就是今天把 Timer / retry_call 单独抽成一个文件的价值。

---

## 明天：D8 第一次真采集

按排期，21 天训练第 1 周到此收尾。接下来先回 84 天主线（M1 W2 Prompt Engineering，Day 8–14），到 **M2 W5（SNMP 协议实战）** 时插入 21 天训练第 2 周，也就是从 D8 接真 pysnmp 开始。

简单说：**D7 是分界线。左边是「能把想法变成能跑的东西」，右边开始学「Agent 到底是什么」。**

---

## 卡住了怎么办

1. 先看第 5 步的对照表
2. 想单验某一项，就临时把 `check_d7.py` 里 `CHECKS` 的其他行注释掉
3. 还是不行，把 `python check_d7.py` 的完整输出贴给我
