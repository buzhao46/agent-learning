# D5 · 第一次写测试

> 今天写的代码不比前四天少，但**性质完全变了**：前四天是"让程序能跑"，今天是"让程序以后改不坏"。
>
> 关键认知：**测试的价值不在写，在于它能发现 bug。** 所以 `check_d5.py` 不数你写了几个测试函数，它会**故意把你被测的代码改坏**，然后看你的测试能不能发现。这叫「变异测试」，是唯一能证明"测试真的有用"的办法。
>
> 只有 `assert True` 的测试、只跑不断言的测试、永远通过的测试，在今天全部会红。

---

## 前置 · 虚拟环境（第一次用，一次性搞定）

D1–D4 你都是直接用系统 Python 跑的，到 D5 不行了 —— 项目依赖要开始隔离。

虚拟环境已经替你建好，pytest 也装好了：

```
week1/.venv/                     ← 虚拟环境（已建）
week1/.venv/Scripts/python.exe   ← 里面的 Python
pytest 9.1.1                     ← 已装好
```

**每天开工第一件事：激活它。**

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1
.venv\Scripts\activate
```

激活成功的标志：命令行前面出现 `(.venv)`。

激活过程中出现的错误：

### 如果报「禁止运行脚本」

```
.venv\Scripts\activate : 无法加载文件 ...\Activate.ps1，因为在此系统上禁止运行脚本。
```

这不是报错，是 Windows PowerShell 的默认安全策略拦住了 `.ps1` 脚本（默认 `Restricted`：任何脚本都不许跑）。三种解法，任选一种：

**方案 A（推荐，一次配好）**

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

`-Scope CurrentUser` 只影响你自己，不需要管理员权限。`RemoteSigned` 的含义是：本地写的脚本可以跑，从网上下载的必须有数字签名。安全性可接受，是 Python 官方文档推荐的做法。

如果执行后提示被组策略覆盖（公司电脑常见），说明 IT 部门锁死了，改用方案 C。

**方案 B（不改任何设置，最保险）**

不激活，直接用 venv 里的解释器。等价于"激活后敲 `python`"：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1
.venv\Scripts\python.exe -m pytest d5 -q
```

**方案 C（用 CMD 而不是 PowerShell）**

```
cmd
```
然后 `cd` 到 `week1`，运行 `.venv\Scripts\activate.bat`（`.bat` 不受 PowerShell 策略限制）。

> ⚠️ 不管用哪种方案，跑 `check_d5.py` 时**必须用 venv 的那个 Python**。因为 `check_d5.py` 内部是用 `sys.executable` 去启动 pytest 的——你用什么 Python 调它，它就用什么 Python 跑测试。用系统 Python 调，起来的就是没装 pytest 的那个，全项失败。
>
> 所以方案 B / C 下，命令要写成 `.venv\Scripts\python.exe ..\d5\check_d5.py`，而不是 `python ..\d5\check_d5.py`。

---

## 一、要做成什么样

```
week1/d5/
├── models.py        ← 从 d4 复制（被测对象，一般不要改）
├── log_setup.py     ← 从 d4 复制
├── cli.py           ← 从 d4 复制
├── devices.json     ← 已有
├── test_device.py   ← 你写（今天的任务）
└── check_d5.py      ← 验收脚本
```

`test_device.py` 里写 6 个测试函数，覆盖 5 个场景 + 1 个往返验证。

---

## 第 0 步 · 把 d4 的代码搬过来

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d5
Copy-Item ..\d4\models.py .
Copy-Item ..\d4\log_setup.py .
Copy-Item ..\d4\cli.py .
```

**自检**：`python cli.py --limit 1` 能跑出表格（此时 `python` 得是激活了 venv 的那个）。

> ⚠️ **今天不要改 `models.py` 的内容。** 它是被测对象，改它就等于改考题。`check_d5.py` 会在它上面做临时变异，做完自动还原。

---

## 第 1 步 · 先让 pytest 跑起来（5 分钟）

新建 `test_device.py`，先只写一个最简单的：

```python
"""D5 · snmpcheck 的单元测试。"""

from models import Device


def test_sanity() -> None:
    d = Device(name="x", ip="1.1.1.1")
    assert d.name == "x"
```

跑：

```powershell
python -m pytest -q
```

看到 `1 passed` 就成了。

**这一步刻意很小**，目的是确认三件事：文件被 pytest 认出来了、`from models import ...` 能导入、断言生效。

如果报 `ModuleNotFoundError: No module named 'models'`，八成是在错误的目录下跑的 —— 必须 `cd d5` 再跑。

---

## 第 2 步 · 补满 6 个测试（主体，约 40 分钟）

下面是每个测试要验什么。**代码你自己写**，只给骨架和提示。

先把文件头的 import 补全：

```python
import json

import pytest

from models import Device, SnmpError, load_devices
```

### ① 正常加载

```python
def test_load_real_json() -> None:
    devices = load_devices(???)      # 提示：传 d5/devices.json 的路径
    assert len(devices) == ???       # 提示：数一下 json 里有几台
    assert devices[0].name == ???    # 提示：看 json 里第一台叫什么
```

打开 `devices.json` 自己数，别看老代码。

### ② 空列表

设备清单是空的，应该返回空 list，而不是报错。

需要一个内容是 `{"count": 0, "devices": []}` 的 json 文件 —— 别手工去建，用 `tmp_path`（见第 3 步的说明）：

```python
import json

def test_load_empty_list(tmp_path) -> None:
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"count": 0, "devices": []}), encoding="utf-8")
    assert load_devices(p) == ???
```

### ③ CPU 边界

这是今天最容易写错的一个。`models.py` 里 `is_healthy` 的判据是 `cpu < cpu_limit`（**严格小于**）。

所以要分别验三档：

```python
def test_cpu_boundary() -> None:
    assert Device(name="a", ip="1.1.1.1", cpu=??).is_healthy() is True    # 刚好低于上限
    assert Device(name="a", ip="1.1.1.1", cpu=??).is_healthy() is False   # 刚好等于上限 ← 关键
    assert Device(name="a", ip="1.1.1.1", cpu=??).is_healthy() is False   # 明显超过
```

默认上限是 80。**中间那条就是 C3 变异要考的东西** —— 如果你只测 50 和 92，把 `<` 改成 `<=` 也发现不了。

另外顺便测一下 `status="down"` 时无论 CPU 多少都不健康。

### ④ 非法 status

`from_dict` 遇到 `status` 不是 `"up"` / `"down"` 时会抛 `SnmpError`。

```python
import pytest

def test_invalid_status_raises() -> None:
    with pytest.raises(SnmpError):
        Device.from_dict({"name": "x", "ip": "1.1.1.1", "status": ???})
```

`pytest.raises(...)` 本身就是断言 —— 没抛异常它就会让测试失败。

### ⑤ 文件不存在

```python
def test_missing_file_raises_snmp_error(tmp_path) -> None:
    with pytest.raises(SnmpError):
        load_devices(tmp_path / "nope.json")
```

注意：这里要的是 `SnmpError`，**不是** `FileNotFoundError`。`load_devices` 内部把后者包装成了前者（`raise ... from exc`）。如果你写 `pytest.raises(FileNotFoundError)` 就会失败 —— 这是个好现象，说明测试在教你代码的真实行为。

### ⑥ 序列化往返（今天最重要的一课）

`to_dict()` 和 `from_dict()` 应该能来回转换不丢信息。听起来简单，但**这里有个隐蔽的陷阱**：

```python
d = Device(name="x", ip="1.1.1.1")
assert Device.from_dict(d.to_dict()) == d      # ← 这行通过，但什么都没验证到
```

为什么？因为 `d` 的 `status` / `cpu` / `tags` 都是**默认值**。就算 `to_dict()` 只返回了 `name` 和 `ip`，`from_dict()` 也会用默认值补齐，两边照样相等。

**用默认值测往返 = 没测。** 必须用非默认值：

```python
def test_roundtrip_keeps_all_fields() -> None:
    src = Device(name="agg-sw-03", ip="10.0.0.3", status=???, cpu=???, tags=???)
    back = Device.from_dict(src.to_dict())
    assert back.name == src.name
    assert back.ip == src.ip
    # 剩下三个字段也要逐个断言 —— 这是重点
```

C6 变异就是把 `to_dict` 改成只返回 `name` / `ip`，只有上面这种写法能抓住它。

---

## 第 3 步 · 跑验收

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d5
python check_d5.py
```

准备检查会先看一眼环境（pytest 在不在、被测代码在不在）。

### 七项分别查什么

| 项 | 查什么 |
|---|---|
| C1 | pytest 能跑，至少 5 个用例，全绿 |
| C2 | 每个 `test_` 函数里都有真断言（不许有 `pass` 的空壳） |
| C3 | 把 `cpu < cpu_limit` 改成 `<=`，你的测试要发现 |
| C4 | 把 status 校验删掉，你的测试要发现 |
| C5 | 把 `except FileNotFoundError` 改成别的，你的测试要发现 |
| C6 | 把 `to_dict` 改成只返回 name/ip，你的测试要发现 |
| C7 | 【加练】有测试用了 `tmp_path` |

### 变异测试是怎么做的

`check_d5.py` 每次会：

1. 备份 `models.py`
2. 用正则把某一行改坏
3. 跑 pytest
4. 恢复 `models.py`
5. 看你红没红

红了 = 你的测试兜住了这个 bug；还是绿 = 这个场景你其实没测到。

**注意：变异前它会先确认"原始代码下测试全绿"。** 如果基线本来就红，改坏之后当然也红，那就分不清是真抓到了还是本来就坏 —— 这是变异测试最容易出现的假阳性，脚本里专门挡了这条。

### 万一中途 Ctrl+C 或超时中断

`models.py` 可能停在变异状态没还原。下次跑会直接告诉你：

```
❌ 准备检查没过：models.py 里有上次检查中断留下的改动
```

修法一条命令：

```powershell
Copy-Item ..\d4\models.py models.py -Force
```

---

## 第 4 步 · 提交

```powershell
python -m pytest -q          # 最后确认全绿
git add .
git status                   # 确认加进去的是 test_device.py 和 d5/*，没有 .venv/__pycache__
git commit -m "D5: 给 snmpcheck 加单元测试（6 例）"
git pushall
```

`git status` 那一步很关键 —— 如果看到 `.venv/` 或 `__pycache__/` 被加了进来，说明 `.gitignore` 没生效（虚拟环境装了上万文件，提交会很惨）。仓库根目录已有正确的 `.gitignore`，正常不该出现。

---

## pytest 速查

| 想做什么 | 怎么写 |
|---|---|
| 被 pytest 发现 | 文件名 `test_*.py` 或 `*_test.py`，函数名 `test_*` |
| 断言 | 直接 `assert x == 1`，不用 `self.assertEqual` |
| 测抛异常 | `with pytest.raises(SnmpError):` |
| 测"不抛" | `pytest.fail("不该走到这里")` |
| 要个临时目录 | 函数参数写 `tmp_path`，pytest 自动给你一个空目录 |
| 要个临时文件 | 用 `tmp_path / "名字.json"` 自己拼路径 |
| 只看结果 | `python -m pytest -q` |
| 第一个失败就停 | `python -m pytest -x` |
| 只跑某个测试 | `python -m pytest -k boundary` |
| 看详细输出 | `python -m pytest -v` |
| 看覆盖率 | 先 `pip install pytest-cov`，再 `python -m pytest --cov=models --cov-report=term-missing`（D20 才要求） |

**不需要 `class`，不需要继承 `unittest.TestCase`。** 一个函数就是一个测试，这是 pytest 相对 Java JUnit 最舒服的地方。

---

## 三个坑

| 现象 | 原因 |
|---|---|
| `ModuleNotFoundError: No module named 'models'` | 没在 `d5` 目录下跑。pytest 把测试文件所在目录加入 `sys.path`，换个目录就找不到 `models` |
| `ModuleNotFoundError: No module named 'pytest'` | venv 没激活，用的是系统 Python |
| `.venv\Scripts\activate` 报「禁止运行脚本」 | PowerShell 执行策略是 `Restricted`。见前面「如果报禁止运行脚本」，改策略或改用 `.venv\Scripts\python.exe` 直接调用 |
| 测试全绿但 C3–C6 全红 | 你的测试只是"跑通了"，没有针对边界/异常/字段完整性做断言。这不是代码错，是测试太松 |

---

## 附加练习（可选，但很值）

**练习一：一个测试只测一件事。**

把 `test_cpu_boundary` 里那三档拆成三个函数：`test_cpu_below_limit` / `test_cpu_at_limit` / `test_cpu_above_limit`。

好处是失败时一眼看出是哪一档坏了，而不是"这个测试挂了，不知道具体哪条断言"。这也是真实项目里测试命名的惯例。

**练习二：给 `save_devices` 也写个测试。**

```python
def test_save_then_load(tmp_path) -> None:
    out = tmp_path / "out" / "d.json"
    save_devices([Device(name="n1", ip="2.2.2.2", cpu=10)], out)
    assert load_devices(out)[0].ip == "2.2.2.2"
```

这条测的是"写完能读回来"，同时验证 `save_devices` 里那句 `path.parent.mkdir(parents=True, exist_ok=True)` 真的管用（`tmp_path/out/` 这个子目录一开始并不存在）。

---

## 明天（D6）预告

D6 是「加重试和计时」：用 `retry_call()` 包住设备采集，用 `Timer` 打印每台设备耗时。

到那时你会第一次发现：**有了测试之后，改代码不用再靠"跑一遍看看"** —— 改完直接 `pytest -q`，3 秒给答案。
