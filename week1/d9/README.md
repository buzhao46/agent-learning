# D9 · 结果转 dataclass + safe_int

> 21 天训练 · 第 2 周「接真数据」第二天
> 预计 50 分钟 · 你要写 4 个东西，约 45 行

---

## 一、今天和昨天有什么不同

D8 你已经**能读回数据**了，但拿在手里的是这个：

```python
{"sys_descr": "Huawei S5735...", "sys_name": "core-sw-01", "uptime": "1 day, 10:17:36.78"}
```

三个问题：

1. **键名是字符串**。`health["up_time"]` 拼错一个字母，只有跑到那一行才知道；IDE 不提示，`mypy` 也查不出来。
2. **值全是 `str`**。`uptime` 已经是格式化好的文本了，原始 ticks 丢了；谁要是想算「运行超过 30 天的设备」，只能重新解析一遍字符串。
3. **没有校验**。设备回 `""`、回 `-1`、回 `4294967295`，你的代码全都照收，然后拿去算、去比大小、去写报告。

今天做两件事：把结果**装进 dataclass**，把数字**过一遍 safe_int**。

三天的分工：

| 天 | 干什么 |
|---|---|
| D8 | 用 `getCmd` 读回三个标量，把错误路径拦住 |
| **D9（今天）** | 结果转 dataclass + 处理异常值（`safe_int`） |
| D10 | 用 `nextCmd` 走 WALK，读接口表 |
| D11 | 落盘 CSV/JSON |
| D12 | 错误分类（才有资格做重试决策） |

今天**不新增 OID、不 WALK、不落盘**。`to_dict()` 只是为 D11 留个口子，不写文件。

---

## 二、环境 & 文件清单

`d9` 目录下现在有五个文件：

| 文件 | 谁写 | 你要动吗 |
|---|---|---|
| `README.md` | 我 | 这份 |
| `snmpio.py` | 我 | **不用改** —— D8 的地基（`snmp_get` + `fmt_ticks`） |
| `fake_agent.py` | 我 | **不用改** —— 假设备，比 D8 版多了一个「脏 uptime」开关 |
| `health.py` | **你** | 4 个 TODO 在这里 |
| `check_d9.py` | 我 | 验收，跑它就行 |

先确认环境还在：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1
.venv\Scripts\python.exe -c "import pysnmp; print(pysnmp.__version__)"
```

要打印 `6.1.4`。

`snmpio.py` 是 D8 的答案版：同样的 `snmp_get`（三道门 + `NoSuchObject`）、同样的 `fmt_ticks`。**今天你在它上面盖楼，不用回头改它。**

> 这就是分层的好处：D8 把「怎么发出一个包」封在 `snmp_get` 里，D9 只关心「拿回来的字符串怎么变成可信的数据」。哪天 pysnmp 换版本、换 v3 认证，只动 `snmpio.py`，`health.py` 一行不改。

---

## 三、第 0 步（5 分钟）：先看清「脏值」长什么样

**不要跳过。** 今天所有的坑都在这一步里。

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d9
..\.venv\Scripts\python.exe -c "
cases = ['12345678', ' 42 ', '1_000', '12.5', '', 'N/A', '-1', '4294967295']
for c in cases:
    try:
        print(f'{c!r:15} -> int() = {int(c)}')
    except Exception as e:
        print(f'{c!r:15} -> {type(e).__name__}: {e}')
print('True -> int() =', int(True))
print('isinstance(True, int) =', isinstance(True, int))
"
```

你会看到（重点看打 ★ 的）：

```
'12345678'       -> int() = 12345678
' 42 '           -> int() = 42              ← 首尾空格能过，没问题
'1_000'          -> int() = 1000     ★      ← Python 认下划线！设备私有 MIB 真有可能回这个
'12.5'           -> ValueError              ← 直接崩
''               -> ValueError      ★      ← 最常见的脏值，设备没配这个字段
'N/A'            -> ValueError      ★      ← 私有 MIB 的占位符
'-1'             -> int() = -1      ★      ← 老设备用 -1 表示「不支持」
'4294967295'     -> int() = 4294967295 ★   ← 计数器溢出 / 「无效」哨兵
True             -> int() = 1       ★      ← bool 是 int 的子类，isinstance 为 True
```

**结论：`int()` 是个「要么成功要么炸」的函数，而 SNMP 的世界里「不干净的值」是常态，不是异常。**

### 设备真实会回的脏值清单

| 设备回的 | 含义 | 应该变成 |
|---|---|---|
| `""` / `"   "` | 字段没配（`sysContact`、`sysLocation` 最常见） | `None`，**不是 0** |
| `"N/A"` / `"--"` / `"unknown"` | 私有 MIB 的占位符 | `None` |
| `-1` | 老设备表示「不支持这个指标」 | `None`（要开关才能放行） |
| `4294967295` / `2147483647` | Counter32 溢出、或「无效」哨兵 | `None` |
| `"12.5"` | 不该出现的浮点 | `None`（**不是 12**，静默截断更危险） |
| `True` | 上游把布尔传下来了 | `None` |
| `NoSuchObject` | D8 已经挡在 `snmp_get` 里 | 不该流到这里 |

> 为什么「不是 0」：`uptime = 0` 表示**这台设备刚重启**，是有意义的真值。把「没采到」也记成 0，报告里就会写「core-sw-01 运行 0 秒」，看起来完全正常，实际上你什么都不知道。**用 0 表示缺失，等于用真值撒谎。**

---

## 四、第 1 步（20 分钟）：`safe_int`（`health.py` 第 38 行）

### 签名

```python
def safe_int(value: object, *, default: int | None = None, allow_negative: bool = False) -> int | None:
```

### 骨架

```python
def safe_int(value, *, default=None, allow_negative=False):
    # ① 先挡 bool —— 它混在 int 里，必须先拦
    if isinstance(value, bool):
        return default
    # ② 真 int：直接走语义检查
    if isinstance(value, int):
        number = value
    # ③ 字符串：strip 之后，只认「可选负号 + 纯数字」
    elif isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"-?\d+", text):
            return default
        number = int(text)
    # ④ 其余一切（None / float / NoSuchObject / list...）一律拒绝
    else:
        return default

    # ⑤ 语义检查：负数和哨兵
    if number < 0 and not allow_negative:
        return default
    if number in SENTINELS:
        return default
    return number
```

`SENTINELS` 我已经定义好了（`frozenset({2147483647, 4294967295})`），`re` 也要自己 import。

### 五个必须想清楚的点

**① `bool` 必须第一个挡**

`isinstance(True, int)` 是 `True`——`bool` 是 `int` 的子类。如果不先挡，`safe_int(True)` 会返回 `1`，然后被当成「设备运行了 0.01 秒」。

这是 Python 的经典陷阱，而且**只在你的上游出 bug 时才会触发**，平时永远碰不到——和 D8 那个 `error_status.prettyPrint()` 是一类坑：**错误路径上的代码，永远用最保守的写法。**

**② 字符串用正则，不用 `int()` + `try`**

写成 `try: return int(text) except ValueError: return default` 也能跑，但会把 `"1_000"` 变成 `1000`、把 `"+42"` 变成 `42`（`+` 号 SNMP 不回，是脏数据的信号）。

用 `re.fullmatch(r"-?\d+", text)` 是**白名单**：只放行我明确知道长什么样的东西。解析外部数据，白名单永远比黑名单安全。

**③ `default` 的默认值是 `None`，不是 `0`**

见第 0 步。类型写成 `int | None` 之后，`mypy` 会**逼着你在每个使用点处理 None** —— 这正是你想要的压力：忘处理就是编译期报错，而不是报告里多一行假数据。

**④ 负数默认拒绝，但留开关**

SNMP 的 Counter / Gauge 是无符号的，负数只可能是「不支持」的哨兵（`-1` 最常见）。所以默认拒绝。

但**真的有合法负数**：温度 MIB 会回 `-5`（零下 5 度）。所以留 `allow_negative=True`，而不是一棍子打死。**「默认安全 + 显式放行」比「默认放行」好。**

**⑤ 哨兵值按类型判，不按大小猜**

你可能想写 `if number > 4_000_000_000: return default`。别这样——`sysUpTime` 跑了几年的设备，ticks 能到几亿甚至几十亿，你砍的是真数据。

判的是**特定的那两个值**（`2**31-1`、`2**32-1`），它们是设备固件的「我没数据」常量。

### 自检

```powershell
..\.venv\Scripts\python.exe -c "
import health as h
print(h.safe_int('12345678'), h.safe_int(' 42 '), h.safe_int(7))
print(h.safe_int(''), h.safe_int('N/A'), h.safe_int('12.5'), h.safe_int('1_000'))
print(h.safe_int(True), h.safe_int(None), h.safe_int(-1))
print(h.safe_int(4294967295))
print(h.safe_int(-5), h.safe_int(-5, allow_negative=True))
print(h.safe_int('', default=0))
"
```

期望：

```
12345678 42 7
None None None None
None None None
None
None -5
0
```

**七行里只要有一行不对，就别往下走。** 后面全部建立在它之上。

---

## 五、第 2 步（10 分钟）：`DeviceHealth`（第 60 行）

骨架我已经给了字段，你写两个 property / method：

```python
@dataclass(frozen=True)
class DeviceHealth:
    host: str
    sys_name: str
    sys_descr: str
    uptime_ticks: int | None = None
    collected_at: float = field(default_factory=time.time)

    @property
    def uptime_text(self) -> str:
        # None → "未知"；有值 → fmt_ticks(...)
        ...

    def to_dict(self) -> dict[str, object]:
        # 给 D11 落盘用的：把 ticks 和文本都带上
        ...
```

### 为什么 `frozen=True`

采集结果是**某一时刻的快照**。允许 `h.uptime_ticks = 0` 这种随手改，会出现「同一台设备在报告里出现两个不同值」的怪事，而且查不出来是谁改的。

要改就用 `dataclasses.replace(h, uptime_ticks=...)` —— 返回新对象，原对象不动。D6 的 `replace(device, cpu=cpu)` 你已经用过一次了。

### 为什么 `uptime_ticks` 是 `int | None`

因为**它就是可能为 None**（第 1 步的结论）。把类型写成 `int` 而实际塞 None，是所有类型注解里最坏的一种：注解在骗人。

`uptime_text` 里 None 的情况要返回 `"未知"`，不能返回 `"00:00:00.00"`（那是「刚启动」，不是「不知道」）。

### 为什么不用 dict 而用 dataclass

| | `dict[str, str]` | `DeviceHealth` |
|---|---|---|
| 键名写错 | 运行时 `KeyError` | 属性不存在，`mypy` 直接报 |
| 类型 | 全是 `str` | `int \| None`、`float`，各归各位 |
| 派生值 | 每个调用点自己算 | `uptime_text` 一处定义 |
| 转 JSON | 手写一遍键 | `to_dict()` 一处定义（D11 直接用） |
| IDE 补全 | 无 | 有 |

多写 15 行，换来后面 D10/D11/D12 每一层都少写一遍解析和校验。**这是「数据结构选对了，代码少一半」的典型。**

### 自检

```powershell
..\.venv\Scripts\python.exe -c "
import health as h
a = h.DeviceHealth(host='10.0.0.1', sys_name='core-sw-01', sys_descr='Huawei', uptime_ticks=12345678)
print(a.uptime_text)
print(a.to_dict())
b = h.DeviceHealth(host='10.0.0.2', sys_name='agg-sw-03', sys_descr='Huawei', uptime_ticks=None)
print(b.uptime_text)
try:
    b.uptime_ticks = 0
except Exception as e:
    print('不可改 ->', type(e).__name__)
"
```

期望：

```
1 day, 10:17:36.78
{'host': '10.0.0.1', 'sys_name': 'core-sw-01', 'sys_descr': 'Huawei', 'uptime_ticks': 12345678, 'uptime_text': '1 day, 10:17:36.78'}
未知
不可改 -> FrozenInstanceError
```

---

## 六、第 3 步（5 分钟）：`build_health`（纯函数分层）

```python
def build_health(host: str, raw: dict[str, str]) -> DeviceHealth:
    """把「三个 OID 的原始字符串」组装成 DeviceHealth。不碰网络。"""
    return DeviceHealth(
        host=host,
        sys_name=raw.get("sys_name", "").strip(),
        sys_descr=raw.get("sys_descr", "").strip(),
        uptime_ticks=safe_int(raw.get("sys_uptime")),
    )
```

**为什么要单独拆出这个函数**（今天最值钱的一条设计）：

`probe_health` 是「发三个包 + 组装」，`build_health` 只有「组装」。分开之后：

- **解析逻辑可以脱离设备测试**。你要验证「设备回 N/A 时怎么办」，不需要真有台设备回 N/A——直接调 `build_health("x", {"sys_uptime": "N/A"})` 就行。C5 就是这么验的。
- **换采集方式时解析逻辑不动**。将来 D10 走 WALK、甚至换成 REST 取数，`build_health` 一行不用改。

这是「IO 层和纯逻辑层分开」，和 D6 把「工具执行」与「模型对话」分开是同一个套路。**能不碰网络就测的代码，才是能测的代码。**

`raw` 的键用 `sys_descr` / `sys_name` / `sys_uptime`（和 D8 的 OID 名字对齐）。用 `.get(k, "")` 而不是 `raw[k]`，是因为**缺键本身也是一种脏输入**——它不该抛 KeyError 把整轮巡检带崩。

---

## 七、第 4 步（10 分钟）：`probe_health` 升级

```python
def probe_health(host, *, community="public", port=161, timeout=1.0) -> DeviceHealth:
    raw = {
        "sys_descr": snmp_get(host, OID_SYSDESCR, community=community, port=port, timeout=timeout),
        "sys_name": snmp_get(host, OID_SYSNAME, community=community, port=port, timeout=timeout),
        "sys_uptime": snmp_get(host, OID_SYSUPTIME, community=community, port=port, timeout=timeout),
    }
    return build_health(host, raw)
```

### 一个必须自己做决定的问题

如果 `sysUpTime` 这个 OID 设备不支持，`snmp_get` 会抛 `ProbeError`（D8 的行为），于是 `probe_health` 整个失败——**但型号和名字其实已经采到了**。

两种选择：

| 做法 | 后果 |
|---|---|
| 让它抛 | 一台设备因为「少一个无关字段」被整轮判失败，报告里它等于离线 |
| 兜住，uptime 记 `None` | 报告里它在线、型号名字都有，uptime 显示「未知」 |

**今天采用第二种**，理由：整机失败是「设备级」判断，字段缺失是「字段级」问题，不该混为一谈。D12 做错误分类时会正式处理这个界限。

要兜的是**只有 uptime**，不是全部三个：

```python
    try:
        raw["sys_uptime"] = snmp_get(...)
    except ProbeError:
        raw["sys_uptime"] = ""      # 采不到 → 让 safe_int 判成 None
```

`sys_descr` / `sys_name` 采不到**一定抛**（那说明设备不可达或 community 错，这是真失败）。

> 注意兜的范围：**只兜你明确决定可以缺失的那个字段**，别写 `try: 三个都采 except: pass`。后者会让「设备根本连不上」也变成一个空壳 `DeviceHealth`，是最难查的一类 bug。

### 自检

```powershell
..\.venv\Scripts\python.exe fake_agent.py            # 另开一个窗口，别关
..\.venv\Scripts\python.exe -c "
import health as h
r = h.probe_health('127.0.0.1', port=1161)
print(type(r).__name__)
print(r.sys_name, '|', r.uptime_ticks, '|', r.uptime_text)
"
```

期望：`DeviceHealth` / `core-sw-01 | 12345678 | 1 day, 10:17:36.78`。

再试脏值那条路（换一个窗口，关掉上面的）：

```powershell
..\.venv\Scripts\python.exe fake_agent.py --port 1162 --uptime-bad
..\.venv\Scripts\python.exe -c "
import health as h
r = h.probe_health('127.0.0.1', port=1162)
print(r.sys_name, '|', r.uptime_ticks, '|', r.uptime_text)
"
```

期望：`core-sw-01 | None | 未知` —— **不能抛异常**。这台设备只是 uptime 字段不可信，它没下线。

---

## 八、第 5 步：跑验收

```powershell
..\.venv\Scripts\python.exe check_d9.py
```

目标 **6/6**（外加练 1 项）。它会自己开端口、自己拉起假设备（含脏 uptime 那台），跑完自己关掉。

### 失败对照表

| 报错 | 原因 |
|---|---|
| C1 `safe_int(' 42 ') 得到 None` | 没 `strip()`，或正则里没允许空格前后（应该在 strip 之后匹配） |
| C2 `safe_int(True) 得到 1` | 没先挡 `bool`，`isinstance(True, int)` 是 True |
| C2 `safe_int('1_000') 得到 1000` | 用了 `try: int(...)` 而不是正则白名单 |
| C2 `safe_int('12.5') 得到 12` | 写了 `int(float(text))` 或 `int(...)` 前先转了 float |
| C3 `safe_int(-1) 得到 -1` | 负数没拦，或 `allow_negative` 没接进判断 |
| C3 `safe_int(4294967295) 得到 4294967295` | 没判 `SENTINELS` |
| C4 `uptime_text 得到 '00:00:00.00'` | None 时没走「未知」分支 |
| C4 `to_dict 缺 'uptime_text'` | `to_dict` 少带一个键（D11 落盘要用的就是它） |
| C4 `居然能改 uptime_ticks` | 没写 `frozen=True` |
| C5 `uptime_ticks 得到 0` | 用了 `default=0`，或者把脏值转成了 0（0 是真值，不能当缺失） |
| C5 `抛了 KeyError` | `build_health` 里用了 `raw["sys_name"]` 而不是 `.get()` |
| C6 `返回的是 dict 不是 DeviceHealth` | `probe_health` 还在返回字典 |
| C6 `脏 uptime 时抛了 ProbeError` | uptime 那条 `snmp_get` 没用 try 兜住 |

---

## 九、如果你手边有真设备

```powershell
..\.venv\Scripts\python.exe -c "
import health as h
r = h.probe_health('192.168.1.1')
print(r.sys_descr)
print(r.sys_name, r.uptime_text)
"
```

真机上今天最可能看到的新鲜事：

| 现象 | 说明 |
|---|---|
| `uptime_text` 是「未知」 | 设备不支持 sysUpTime，或者回的是 `-1` / 空串。**这就是 safe_int 在替你挡枪** |
| `uptime` 几百天 | 正常，机房设备常年不重启 |
| `sys_descr` 是空串 | 有些设备不填这一项，`to_dict()` 里会是 `""` |
| 抛 `ProbeError` | 不可达 / community 错，D8 已分类过，D12 会正式处理 |

可以顺手试 `sysContact`（`1.3.6.1.2.1.1.4.0`）——**它十有八九是空串**，是观察脏值最好的样本：

```powershell
..\.venv\Scripts\python.exe -c "
from snmpio import snmp_get
import health as h
print(repr(snmp_get('192.168.1.1', '1.3.6.1.2.1.1.4.0')))
print(h.safe_int(snmp_get('192.168.1.1', '1.3.6.1.2.1.1.4.0')))
"
```

---

## 十、收尾（5 分钟）

**1. 目录里应该只有五个文件**

```
health.py      ← 你写的
snmpio.py      ← 我给的（D8 地基）
fake_agent.py  ← 我给的
check_d9.py    ← 我给的
README.md      ← 这份
```

第 0 步的试验代码别存文件；`__pycache__` 会被 `.gitignore` 排掉。

**2. 提交**

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35
git add week1/d9
git status
git commit -m "D9: 采集结果转 DeviceHealth，safe_int 兜住脏值"
git pushall
```

---

## 十一、今天真正学到的东西

| 今天做的 | 通用说法 |
|---|---|
| `safe_int` 用正则白名单 | **外部数据只放行你确认过的形状，别指望 try/except** |
| 先挡 `bool` 再挡 `int` | **子类型会伪装成父类型，判类型要从最具体的开始** |
| 缺失用 `None` 不用 `0` | **「没采到」和「测到 0」是两回事，别用真值表示缺失** |
| `int \| None` 逼调用方处理 | **把「可能没有」写进类型，让类型系统替你检查** |
| 哨兵值按值判，不按大小砍 | **裁剪真数据比放过脏数据更糟糕** |
| `frozen=True` | **快照数据不该被随手改** |
| `build_health` 不碰网络 | **IO 和纯逻辑分开，纯逻辑才能低成本测试** |
| 只兜 uptime，不兜三个 | **降级要精确到字段，一把 try 兜住全部等于没有降级** |

最后一条值得多想一层：做 Agent 时，工具函数如果笼统地 `except Exception: return "采集失败"`，模型拿到的就是一个模糊信号，它会在这个基础上继续推理——**噪声比缺失更贵**。精确到字段的降级（`sys_name` 有、`uptime` 无）才能让上层做出正确判断。

---

## 十二、和 snmpcheck 的关系（先知道，别动手）

进度：

```
D8  独立练 getCmd
D9  结果转 dataclass + safe_int        ← 今天
D10 WALK 接口表（ifTable）
D11 落盘 CSV/JSON（to_dict 就是为这天准备的）
D12 错误分类（才有资格做重试决策）
     ↓
     这时才把 snmpcheck/probe.py 的模拟通道换成真采集
```

`snmpcheck/models.py` 里的 `Device` 现在还是 `{name, ip, status, cpu, tags}`。D11 之后它会长出新字段（`sys_descr`、`uptime_ticks` 之类），**今天先不动它**。

你现在该记住的就一句：**`snmp_get` 负责「拿到字符串」，`safe_int` 负责「判断这个字符串能不能信」，`DeviceHealth` 负责「让这件事的类型是明确无误的」。** 三层各管一件事，D10 加 WALK 时你只会动第一层。
