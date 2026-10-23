# D4 · 拆文件：一个 235 行的脚本 → 三个各管一摊的模块

> 今天**不写任何新功能**。行为一个字都不能变，只改代码的组织方式。  
> 这种活叫**重构**。重构有一条铁律：**输出不能有任何变化**。  
> `check_d4.py` 的 C4 就是干这个的 —— 它会真的跑一遍 `../d3/loader.py`，把你的 `cli.py` 输出跟它逐字节对比。改坏一个空格都过不了。

---

## 一、要做成什么样

```
week1/d4/
├── models.py      数据层：Status / SnmpError / Device / load_devices / save_devices
├── log_setup.py   基础设施层：LOG_FILE / setup_logger / get_log
└── cli.py         入口层：argparse / print_table / main
```

依赖方向只能单向：

```
        cli.py
        │    │
        │    └──────────┐
        ↓               ↓
     models.py ────→ log_setup.py
```

- `models.py` 可以用 `log_setup`，但**不许 import `cli`**
- `log_setup.py` **谁也不 import**（它是地基）
- 倒过来就是循环导入，早晚炸

---

## 二、先记住行号分布

复制过来的 `cli.py` 一开始跟 `d3/loader.py` 完全一致，共 235 行：

| 行号      | 内容                                              | 最终归属        |
| ------- | ----------------------------------------------- | ----------- |
| 1–19    | docstring                                       | 最后改写成 cli 的 |
| 20–28   | imports                                         | 三个文件各自挑     |
| 30–34   | `Status` / `HERE` / `DEFAULT_FILE` / `LOG_FILE` | 拆开，见第 2 步   |
| 37–38   | `SnmpError`                                     | models      |
| 42–92   | `setup_logger`                                  | log_setup   |
| 95–107  | `_LOG` / `get_log`                              | log_setup   |
| 111–137 | `Device`                                        | models      |
| 141–163 | `load_devices` / `save_devices`                 | models      |
| 166–180 | `print_table`                                   | cli         |
| 184–230 | `main`                                          | cli         |
| 233–234 | `__main__`                                      | cli         |

**心法：一次只搬一块，搬完立刻跑一次。** 千万别一口气全剪完再跑 —— 那样出错时你不知道是哪一步弄坏的。

---

## 第 0 步 · 准备（约 2 分钟）

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d4
Copy-Item ..\d3\loader.py cli.py
python cli.py --limit 2 --verbose
```

**自检**：能跑出表格和日志，说明起点是对的。

> ⚠️ 这一步**先别动 docstring**（开头那 19 行说明）。保持文件原样，下面的行号才对得上。等全拆完再统一改。

> ⚠️ **不要修改 `../d3/loader.py`**。它是 C4 的对照基准，改了基准等于自己给自己放水。

---

## 第 1 步 · 切出 `log_setup.py`

**为什么先切它**：它最独立，不依赖任何别的东西。先切最容易的，建立手感。

新建 `log_setup.py`，从 `cli.py` 里把这四段搬进去。

### ① 头部

```python
from __future__ import annotations

import logging
import sys
from pathlib import Path
```

只需要 `logging` / `sys` / `Path`。`argparse`、`json`、`dataclass`、`typing` 都不需要。

### ② 常量（原第 34 行）

```python
HERE = Path(__file__).parent
LOG_FILE = HERE / "out" / "run.log"
```

### ③ 两个函数（原 42–92 行 + 95–107 行，原样搬，一个字别改）

```python
def setup_logger(level: int = logging.INFO, logfile: Path | None = LOG_FILE) -> logging.Logger:
    ...  # 原样

_LOG: logging.Logger | None = None


def get_log(level: int = logging.INFO) -> logging.Logger:
    ...  # 原样
```

### ④ 模块 docstring（新写，别抄旧的）

```python
"""日志配置：谁来 import 都拿到同一个 logger，且只在第一次真正使用时才建 handler。"""
```

### ⑤ 回去改 `cli.py`

把刚剪掉的三段删掉，在 import 区加一行：

```python
from log_setup import get_log
```

### 自检

```powershell
python -c "from log_setup import get_log; get_log().info('hello')"
```

应该看到一行 `INFO | snmpcheck | hello`。报 `ModuleNotFoundError` 就是文件存错位置了。

```powershell
python cli.py --limit 2 --verbose
```

输出必须跟第 0 步**一模一样**。不一样就回退重来。

---

## 第 2 步 · 切出 `models.py`

> ⚠️ 上一步删了行，现在 `cli.py` 已经不是 235 行了。**按函数名找，别按行号。**

新建 `models.py`。

### ① 头部

```python
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from log_setup import get_log
```

`from log_setup import get_log` 这行必须有 —— `load_devices` 里那句 count 不一致的 warning 要用它。

### ② 常量和类型别名

```python
Status = Literal["up", "down"]

HERE = Path(__file__).parent
DEFAULT_FILE = HERE / "devices.json"
```

**这里有个设计决策，想清楚再写**：`DEFAULT_FILE` 放 `models.py` 还是 `cli.py`？

答案是 **`models.py`**。理由：它描述的是"数据文件在哪"，属于数据层的知识。`cli.py` 要用就 `from models import DEFAULT_FILE`。

更要紧的是 `HERE = Path(__file__).parent` 这一句 —— `__file__` 是"当前这个文件"的路径。写在 `models.py` 里算出来是 `d4\`，写在 `cli.py` 里也是 `d4\`。今天三个文件同目录，结果一样，但概念上必须分清：**常量定义在"最懂它"的那一层**。

### ③ 搬这些定义（原样，一字不改）

- `class SnmpError(RuntimeError)`
- `@dataclass class Device`（含 `is_healthy` / `to_dict` / `from_dict`）
- `def load_devices(...)`
- `def save_devices(...)`

### ④ 模块 docstring

```python
"""数据层：设备的数据结构与磁盘读写。不掺命令行，也不往屏幕输出。"""
```

### 自检

```powershell
python -c "from models import Device; d = Device.from_dict({'name':'x','ip':'1.1.1.1','cpu':30}); print(d.is_healthy())"
```

要打印 `True`。

```powershell
python -c "import models"
```

**必须什么也不输出**。如果这行建出了 `out\run.log`，说明你在 `models.py` 顶层调了 `get_log()` —— 那就是 C6 要抓的。

---

## 第 3 步 · 收拾 `cli.py`

现在 `cli.py` 应该只剩：docstring、`print_table`、`main`、`__main__`。

把 import 区整理成这样：

```python
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from log_setup import get_log
from models import DEFAULT_FILE, Device, SnmpError, load_devices
```

几个细节：

- `logging` **要留** —— `main()` 里 `logging.DEBUG` / `logging.INFO` 还在用
- `Device` 要 import —— `print_tabices: list[Device])` 的注解用到它
- `DEFAULT_FILE` 要 import —— `parser.add_argument("--file", default=str(DEFAULT_FILE))` 用到它
- `HERE` **不用** import，`cli.py` 里已经没有地方用它了
- 如果 `DEFAULT_FILE` 你**留在了 `cli.py`** 没搬到 models，那这行 import 就删掉它

### 自检

```powershell
python cli.py --limit 2 --verbose
python cli.py --device core-rtr-02
python cli.py --limit 0
```

前两条正常输出，第三条退出码 2 并提示 limit。跟 D3 表现一致。

---

## 第 4 步 · 跑验收

```powershell
cd week1\d4
python check_d4.py
```

目标 **7/7**。照 ❌ 的提示改，每一条都写了具体原因。

基线（什么都没做）是 **1/7**，只有 C3「无反向依赖」白捡着过。

### 七项分别查什么

| 项  | 查什么                                                                              |
| -- | -------------------------------------------------------------------------------- |
| C1 | 三个文件都在，而且不是空壳                                                                    |
| C2 | `models.py` 里没有 `argparse` / `logging` / `print`                                 |
| C3 | 下层没有反向 import `cli`                                                              |
| C4 | `--limit 2 --verbose` 的 stdout **和** 日志，跟 D3 逐字节一致                               |
| C5 | 外部能 `from models import Device` 单独用（构造 / `to_dict` 往返 / 非法 status 抛 `SnmpError`） |
| C6 | `python -c "import models"` 不打印、不创建 `out/run.log`                                |
| C7 | 【加练】`python -m cli --limit 1` 也能跑                                                |

### 跑不过时的对照表

| 报错                                    | 原因                                                                          |
| ------------------------------------- | --------------------------------------------------------------------------- |
| C2 说 `models.py 第 N 行有 print(...)`    | 你把 `print_table` 或者调试用的 print 一起搬进 models 了。它属于 cli                         |
| C2 说 `不该 import sys`                  | `models.py` 里还留着 `import sys`。它只被 `setup_logger` 用，已经搬走了                    |
| C3 说 `反过来 import 了 cli`               | `models.py` 里写了 `import cli`。**依赖只能单向**：cli → models，不能反过来                  |
| C4 说 `stdout 和 D3 不一致`                | 多半是搬 `print_table` 时手误动了 f-string 的列宽或空格。**原样复制，别"顺手优化"**                   |
| C5 说 `单独 import models 用不了`           | `models.py` 缺 `from log_setup import get_log`，或者 `SnmpError` / `Device` 没搬全 |
| C6 说 `import models 就创建了 out/run.log` | `models.py` 顶层直接调了 `get_log()`。logger 只在 `log_setup` 里建，models 只该"用"不该"建"   |

---

## 第 5 步 · 清理与提交

三个文件都定下来之后，回头做这几件收尾。

### ① 改掉 `cli.py` 的 docstring

开头那 19 行是 D3 的任务说明书（讲 `--limit` / `--verbose` 要实现什么），任务做完了留着会误导人。换成：

```python
"""D4 · 命令行入口：解析参数、编排流程、输出表格。

    python cli.py --limit 2 --verbose
    python cli.py --device core-sw-01
"""
```

### ② 删掉 `main()` 里那段 `try/except NotImplementedError`

这是 D2 留下的脚手架（"还没写 `setup_logger` 时给个提示"）。函数早就写好了，这段永远不触发，属于死代码：

```python
    try:
        level = logging.DEBUG if args.verbose else logging.INFO
        log = get_log(level)
    except NotImplementedError as exc:
        sys.stderr.write(...)      # ← 整段删
        return 3
```

删成两行：

```python
    level = logging.DEBUG if args.verbose else logging.INFO
    log = get_log(level)
```

注意：原来 `sys.stderr.write` 那行占着 `sys` 的用途。删完检查一下 —— 最后一行 `sys.exit(main())` 还在用，所以 `import sys` **保留**。

### ③ 再跑一次

```powershell
python check_d4.py
```

删死代码不该影响任何输出，7/7 要保持不变。如果 C4 红了，说明你删多了。

### ④ 提交

```powershell
git add .
git status
git commit -m "D4: 拆成 models/log_setup/cli 三层"
git pushall
```

`git status` 那一步要确认看到的是**新增的三个 .py**，而不是只剩一个改动过的文件。

---

## 补充 · `python cli.py` 和 `python -m cli` 的区别

C7 是加练，但值得理解，后面做真实项目天天用。

```powershell
python cli.py      # 按"文件路径"执行：sys.path[0] = d4\
python -m cli      # 按"模块名"执行：让 Python 自己去 sys.path 里找 cli.py
```

今天两种都能跑，因为三个文件平铺在同一目录。差别会在两种情况下暴露：

**情况一：`cli.py` 里有相对导入。** 如果你写成 `from .models import Device`（带点），`python cli.py` 会报 `ImportError: attempted relative import with no known parent package`，只有 `python -m cli` 才行。**所以今天三个文件都用不带点的绝对导入**，两种跑法都兼容。

**情况二：文件在子目录里。** 一旦你把它们放进 `week1/d4/snmp/`，`python snmp/cli.py` 就找不到 `models` 了（`sys.path[0]` 变成了 `snmp/`），而 `python -m snmp.cli` 仍然可以。

记一条经验：**入口脚本用 `python -m 包.模块` 启动，比 `python 路径.py` 更不容易出问题。** 这也是真实项目里几乎看不到 `python xxx.py` 的原因。

---

## 三个坑

| 现象                                                                           | 原因                                                                                                                          |
| ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| 拆完 `mypy` 报 `Duplicate module named 'loader'`                                | 你在 `week1/` 根目录跑了 `mypy .`。d1/d2/d3 都有 `loader.py`，mypy 按文件名当模块名就撞了。**在各自目录里跑** `mypy models.py cli.py`                     |
| `ImportError: cannot import name 'Device' from partially initialized module` | 循环导入。多半是 `models.py` 里写了 `import cli`，或者两个文件互相 `from` 对方                                                                    |
| 拆完日志打了**两遍**                                                                 | 你在两个文件里各调了一次 `logging.getLogger("snmpcheck")` 并各自 `addHandler`。这个 logger 是全局单例，handler 会累加。日志配置**只留一份**，就在 `log_setup.py` 里 |

---

## 这一步在真实项目里的意义

你现在做的拆法，就是 Python 项目最常见的三层结构：

- **`models.py`（数据层）** —— 后面换成 SQLAlchemy 的 ORM 模型、Pydantic 的 Schema，位置完全一样
- **`log_setup.py`（基础设施层）** —— 后面会加 `config.py`、`db.py`，都是这一层
- **`cli.py`（入口层）** —— 后面变成 `main.py` + FastAPI 的 router，或者换成 Agent 的 tool 注册入口

D1 到 D3 你在**写功能**，D4 开始你在**管结构**。从这一步起，代码量不再是唯一指标 —— 能不能让人一眼看出"哪块该改"，才是。

---

## 明天（D5）预告

D5 是「第一次测试」：给现在这套代码写 5 个 pytest 用例 —— 正常查 / 未知设备 / 空列表 / CPU 边界 / 文件不存在。

`check_d5.py` 会用**故意改坏你的代码**的方式，验证你的测试真的能抓到 bug（而不是一堆永远通过的空断言）。
