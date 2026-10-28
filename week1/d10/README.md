# D10 · WALK 接口表

**今天一句话**：昨天读的是「我知道我要什么」的三个固定 OID，今天读的是一张**行数事先不知道**的表。

设备有几个接口、每个接口叫什么，只有问了才知道。这就是 WALK。

预计 60–70 分钟。四个 TODO，约 60 行。

---

## 一、先搞清楚 WALK 到底在做什么

SNMP 没有「读整张表」这个操作。只有两个原语：

| 原语 | 意思 |
|---|---|
| GET | 我要 `1.3.6.1.2.1.2.2.1.2.3` 这个值 |
| GETNEXT | 我给你一个 OID，你把**比它大的下一个**给我 |

WALK 就是用 GETNEXT 一步步挪：

```
GETNEXT 1.3.6.1.2.1.2.2.1.2      → 1.3.6.1.2.1.2.2.1.2.1  = "GE0/0/1"
GETNEXT 1.3.6.1.2.1.2.2.1.2.1    → 1.3.6.1.2.1.2.2.1.2.2  = "GE0/0/2"
GETNEXT 1.3.6.1.2.1.2.2.1.2.2    → 1.3.6.1.2.1.2.2.1.2.3  = "上联口-核心"
GETNEXT 1.3.6.1.2.1.2.2.1.2.3    → 1.3.6.1.2.1.2.2.1.2.4  = "XGE0/0/4"
GETNEXT 1.3.6.1.2.1.2.2.1.2.4    → 1.3.6.1.2.1.2.2.1.3.1  ← 跑到 ifType 去了，停
```

**问题就在最后一行**：设备不知道你想读哪张表，它只会「给下一个」。
所以「什么时候停」必须由你自己判断 —— 停在 `1.3.6.1.2.1.2.2.1.2` 这棵子树外面。

这就是 `lexicographicMode=False` 的作用。不传它，你会一路读到整个 MIB 的末尾。

---

## 二、今天的目录

```
week1/d10/
├── ifwalk.py       ← 你写，4 个 TODO
├── snmpio.py       ← D9 的，原样复制（snmp_get / fmt_ticks / ProbeError）
├── health.py       ← D9 的成品，今天用不到，留着给 D11 用
├── fake_agent.py   ← 我写的假交换机，今天它能应答 WALK 了
├── check_d10.py    ← 7 项验收
└── README.md       ← 这份
```

**不要改** `fake_agent.py`、`snmpio.py`、`check_d10.py`。改了就失去对照基准。

`ifwalk.py` 里已经写好的：

| 行 | 内容 | 说明 |
|---|---|---|
| 77 | `disp_width()` | 按显示宽度算长度（中文占 2 格） |
| 86 | `pad()` | 按显示宽度补空格 |
| 48–57 | `OPER_STATUS` | ifOperStatus 的七种取值 |
| 188 | `main()` | CLI，已写好 |

你要填的四个：

| 行 | 函数 | 行数估计 |
|---|---|---|
| 128 | `walk` | ~25 行（核心） |
| 138 | `if_index` | 2 行 |
| 160 | `build_interfaces` | ~15 行 |
| 184 | `fmt_table` | ~12 行 |

---

## 三、第 0 步：先亲眼看一次（5 分钟，别跳过）

今天有一半的坑在这一步。

### 0.1 起一台假交换机

开一个 PowerShell 窗口，**这个窗口别关**：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d10
..\.venv\Scripts\python.exe fake_agent.py --port 1161
```

看到这行就对了：

```
假设备已启动：127.0.0.1:1161  name=core-sw-01  community=public
接口数 4  miss_status=False  loop_bug=False  many=False
```

### 0.2 看两个函数的返回类型（10 秒）

```powershell
..\.venv\Scripts\python.exe -c "import inspect; from pysnmp.hlapi import nextCmd, walkCmd; print('nextCmd ->', inspect.signature(nextCmd).return_annotation); print('walkCmd ->', inspect.signature(walkCmd).return_annotation)"
```

```
nextCmd -> tuple[errind.ErrorIndication, Integer32 | int, Integer32 | int, tuple[ObjectType]]
walkCmd -> typing.Generator[ForwardRef('tuple[errind.ErrorIndication, ...]'), NoneType, NoneType]
```

`nextCmd` 返回 **tuple**（跑一次就结束），`walkCmd` 返回 **Generator**（能一直往下拿）。

> **今天第一个反直觉的地方**：pysnmp 6.1.4 里 `nextCmd` 只走**一步**，
> `walkCmd` 才是完整 WALK。网上 4.x/5.x 的教程正好相反（那时 `nextCmd` 就是生成器）。
>
> 照旧教程写 `for ei, es, i, vs in nextCmd(...)`，会拿到
> `ValueError: not enough values to unpack` —— 因为你在对单个结果做遍历。

### 0.3 亲眼看见「越界」

先**不带** `lexicographicMode`：

```powershell
..\.venv\Scripts\python.exe -c "from pysnmp.hlapi import *; e=SnmpEngine(); g=walkCmd(e, CommunityData('public'), UdpTransportTarget(('127.0.0.1',1161), timeout=1.0, retries=0), ContextData(), ObjectType(ObjectIdentity('1.3.6.1.2.1.2.2.1.2'))); rows=[(str(n), v.prettyPrint()) for _,_,_,vs in g for n,v in vs]; print(len(rows), '行'); [print(' ', r) for r in rows]"
```

输出 **20 行**：

```
20 行
  ('1.3.6.1.2.1.2.2.1.2.1', 'GE0/0/1')
  ('1.3.6.1.2.1.2.2.1.2.2', 'GE0/0/2')
  ('1.3.6.1.2.1.2.2.1.2.3', '0xe4b88ae88194e58fa32de6a0b8e5bf83')
  ('1.3.6.1.2.1.2.2.1.2.4', 'XGE0/0/4')
  ('1.3.6.1.2.1.2.2.1.3.1', '6')                ← 已经跑到 ifType 了
  ...
  ('1.3.6.1.2.1.4.20.1.2.192.168.1.1', '2')     ← 连 IP 地址表都读进来了
```

我要的是 `ifDescr` 这一列（4 行），它给了我整棵 MIB（20 行）。

再加上 `lexicographicMode=False`：

```powershell
..\.venv\Scripts\python.exe -c "from pysnmp.hlapi import *; e=SnmpEngine(); g=walkCmd(e, CommunityData('public'), UdpTransportTarget(('127.0.0.1',1161), timeout=1.0, retries=0), ContextData(), ObjectType(ObjectIdentity('1.3.6.1.2.1.2.2.1.2')), lexicographicMode=False); rows=[(str(n), v.prettyPrint()) for _,_,_,vs in g for n,v in vs]; print(len(rows), '行'); [print(' ', r) for r in rows]"
```

才是 **4 行**。

> 本机的假设备只有 20 个 OID，真设备的 MIB 有**上万条**。
> 少了这一个参数，一台 48 口交换机的巡检会从 2 秒变成 2 分钟。

### 0.4 看两个类型的取值差异

```powershell
..\.venv\Scripts\python.exe -c "from pysnmp.hlapi import *; e=SnmpEngine(); g=walkCmd(e, CommunityData('public'), UdpTransportTarget(('127.0.0.1',1161), timeout=1.0, retries=0), ContextData(), ObjectType(ObjectIdentity('1.3.6.1.2.1.2.2.1.2')), lexicographicMode=False); [print(repr(v.prettyPrint()), '|', type(v).__name__, '|', (repr(v.asOctets().decode('utf-8','replace')) if isinstance(v, OctetString) else '(非字符串)')) for _,_,_,vs in g for n,v in vs]"
```

你会看到：

```
'GE0/0/1'                            | OctetString | 'GE0/0/1'
'GE0/0/2'                            | OctetString | 'GE0/0/2'
'0xe4b88ae88194e58fa32de6a0b8e5bf83' | OctetString | '上联口-核心'      ← 注意这行
'XGE0/0/4'                           | OctetString | 'XGE0/0/4'
```

**第 3 行就是今天第二个坑**：中文接口名，`prettyPrint()` 给你一串十六进制。
直接打进报告，运维看到的是 `0xe4b88ae8...`，不是「上联口-核心」。

第 3 列说明 `asOctets().decode("utf-8")` 能正确解出来。

再把 OID 换成 `1.3.6.1.2.1.2.2.1.8`（ifOperStatus）跑一次，
你会发现 `asOctets()` 对 Integer 没用 —— 它给的是原始字节，不是数字文本。
所以**必须按类型分流**，不能一律用某一种。

---

## 四、第 1 步：`walk`（第 128 行，25 分钟）

这是今天的核心。结构是：

```python
rows: list[tuple[str, str]] = []
for error_indication, error_status, _error_index, var_binds in walkCmd(
    SnmpEngine(),                                     # ①
    CommunityData(community),
    UdpTransportTarget((host, port), timeout=timeout, retries=0),
    ContextData(),
    ObjectType(ObjectIdentity(base_oid)),
    lexicographicMode=False,                          # ②
):
    if error_indication or error_status:              # ③
        ...
    for name, value in var_binds:
        text = ...                                    # ④
        rows.append((str(name), text))
    if len(rows) >= max_rows:                         # ⑤
        ...
return rows
```

### ① `SnmpEngine()` 每次新建，别提到模块级

**这条我实测过，是真的会卡死**：同一个 `SnmpEngine` 跑第二次 `walkCmd`，
程序就永远不动了——不报错、不超时、不退出，只能 Ctrl+C。

而 D9 的 `snmpio.py` 里复用 `_ENGINE` 做多次 `getCmd` 是完全正常的。
**GET 可以复用，WALK 不行**，这两种情况在 pysnmp 6.1.4 里不一样。

所以 `ifwalk.py` 里我刻意没给你全局 `_ENGINE`（第 58 行有一段说明）。

### ② `lexicographicMode=False`

不写它，`walkCmd` 会一路读到 MIB 末尾。C2 专门测这个：期望 4 行，实际会 20 行。

### ③ 错误要从四元组里取

`walkCmd` 每轮给 4 个东西，和 `getCmd` 一样：`error_indication`（网络层）、
`error_status`（设备拒绝）、`error_index`、`var_binds`。

**出错后的处理策略，这是今天要你自己做的决定**：

| 情况 | 怎么做 | 为什么 |
|---|---|---|
| 一行还没拿到就出错 | `raise ProbeError(...)` | 设备级失败：不可达、community 错，这不是「表为空」 |
| 已经拿到若干行后出错 | 打一句 warning，`break`，返回已有的行 | 半张表也是数据，最后一行坏了不该全丢 |
| 拿到 `max_rows` 行 | 打一句 warning，返回 | 这是「我们主动停」，不是失败 |

第三种和第二种看起来都是「提前结束」，但语义不同：前者是正常截断，后者是设备异常。

### ④ 取值要按类型分流

```python
text = (
    value.asOctets().decode("utf-8", "replace")
    if isinstance(value, OctetString)
    else value.prettyPrint()
)
```

`OctetString` 已经 import 好了（第 33 行）。

### ⑤ 行数上限不是「省内存」

`max_rows` 存在的真正理由：**保证这个循环一定会结束**。

真实网络里出过这种事——某型号设备在表末尾回一个「不比请求大」的 OID。
客户端如果设了 `ignoreNonIncreasingOid=True`（为了不丢掉这个包），就会原地打转，
永远读同一条，永远不停。

`fake_agent.py --loop` 就是模拟这个 bug。C3 用 20 秒超时判它：**没停下来就算失败**。

### 自检

```powershell
..\.venv\Scripts\python.exe -c "import ifwalk as w; r=w.walk('127.0.0.1','1.3.6.1.2.1.2.2.1.2',port=1161); print(len(r), r)"
```

应该输出 `4 [...四个接口...]`，而且第 3 个是 `上联口-核心` 不是十六进制。

---

## 五、第 2 步：`if_index`（第 138 行，2 分钟）

从 `"1.3.6.1.2.1.2.2.1.2.7"` 里取出 `7`。

```python
tail = oid[len(base) + 1 :]     # → "7"
return int(tail.split(".")[0])  # → 7
```

**别写 `split(".")[-1]`。** 它在 ifTable 上碰巧对（行号只有一段），
但 IP 地址表 `1.3.6.1.2.1.4.20.1.1.10.20.30.1` 的行号是四段，
`[-1]` 给你 `1`，正确的是 `10`。C4 的第三组用例就是它。

### 自检

```powershell
..\.venv\Scripts\python.exe -c "import ifwalk as w; print(w.if_index('1.3.6.1.2.1.4.20.1.1.10.20.30.1','1.3.6.1.2.1.4.20.1.1'))"
```

要打印 `10`。

---

## 六、第 3 步：`build_interfaces`（第 160 行，15 分钟）

把三列合并成一张表。

### 今天的第二个坑：绝对不能用 `zip`

```python
# ❌ 这样写会错位，而且错得很安静
for name, status, octets in zip(descr, status, octets):
```

为什么错：设备可能少回某一行。`fake_agent.py --miss-status` 就模拟了这种情况
（第 3 个接口没有 ifOperStatus，因为接口 down 且处于 notPresent 状态，真实固件会这样）。

于是 `descr` 有 4 行、`status` 只有 3 行：

```
zip 的结果：           正确的结果：
GE0/0/1  ← up          GE0/0/1  ← up
GE0/0/2  ← up          GE0/0/2  ← up
上联口-核心 ← up  ❌     上联口-核心 ← 未知   ← 缺的那行给「未知」
XGE0/0/4 ← 1（少了一行） XGE0/0/4  ← up
```

左边那张表**看起来完全正常**：names 对、status 全是合法值、没有报错。
但「上联口-核心」明明 down，报告里写着 up —— 这就是最难查的一类 bug。

### 正确做法

```python
name_by_index   = { if_index(oid, OID_IF_DESCR):  text  for oid, text in descr }
status_by_index = { if_index(oid, OID_IF_OPER):   ???   for oid, text in status }
octets_by_index = { if_index(oid, OID_IF_IN_OCTETS): int(text) for oid, text in octets }

return [
    Interface(
        index=index,
        name=name,
        status=status_by_index.get(index, "未知"),   # ← 缺了就是「未知」
        in_octets=octets_by_index.get(index),        # ← 缺了就是 None
    )
    for index, name in sorted(name_by_index.items())
]
```

三个要点：

1. **以 `descr` 为准**：接口名是这张表的主键，没有名字的行没有意义
2. **缺 status 给「未知」，不给 `"down"`**：猜错会误报告警，比不报更糟
3. **status 要过 `OPER_STATUS` 映射**：1→up、2→down、3→testing……直接打 `1`/`2` 不算完成

`OPER_STATUS` 是 `dict[int, str]`，所以要把字符串转 int 再查：
`OPER_STATUS.get(int(text), f"未知({text})")`。第二个参数兜住枚举外的怪值。

### 自检

先起一台**缺状态行**的设备（另开一个窗口）：

```powershell
..\.venv\Scripts\python.exe fake_agent.py --port 1162 --miss-status
```

然后验合并结果：

```powershell
..\.venv\Scripts\python.exe -c "import ifwalk as w; d=w.walk('127.0.0.1', w.OID_IF_DESCR, port=1162); s=w.walk('127.0.0.1', w.OID_IF_OPER, port=1162); o=w.walk('127.0.0.1', w.OID_IF_IN_OCTETS, port=1162); print('descr', len(d), 'status', len(s), 'octets', len(o)); [print(r) for r in w.build_interfaces(d, s, o)]"
```

要看到 `descr 4 status 3 octets 4`，然后 4 行 Interface，第 3 个 `status='未知'`、
`in_octets=4294967100`（**不是**第 4 个接口的 512004）。

如果第 3 行的 status 是 `'up'`，说明拿了下一行的值顶上来 —— 那就是 `zip` 的毛病。

或者直接用 CLI 看（1162 那台还在跑）：

```powershell
..\.venv\Scripts\python.exe ifwalk.py --port 1162
```

第 3 个接口的状态应该是 `未知`，第 4 个仍是 `up`（没被顶掉）。

---

## 七、第 4 步：`fmt_table`（第 184 行，15 分钟）

### 今天的第三个坑：中文占两格

```python
len("上联口-核心")   # 5
disp_width("上联口-核心")  # 11  ← 终端里实际占的格数
```

用 `text.ljust(width)` 补齐，中文会少补一半空格，整张表从那一行开始歪。

`pad()`（第 86 行）已经按显示宽度算好了，直接用。

### 目标输出

```
索引  接口名       状态  入字节
----  -----------  ----  ----------
1     GE0/0/1      up    1284993201
2     GE0/0/2      up    88120442
3     上联口-核心  down  4294967100
4     XGE0/0/4     up    512004
```

### 结构

```python
header = ("索引", "接口名", "状态", "入字节")
body = [
    (str(row.index), row.name, row.status, "-" if row.in_octets is None else str(row.in_octets))
    for row in rows
]

# 列宽 = 这一列（含表头）里最长的那个的显示宽度
width = [max(disp_width(row[i]) for row in (header, *body)) for i in range(len(header))]

def line(cells):
    return "  ".join(pad(cell, width[i]) for i, cell in enumerate(cells)).rstrip()

out = [line(header), line(tuple("-" * w for w in width))]
out.extend(line(row) for row in body)
return "\n".join(out)
```

三个细节：

- **表头也算进列宽**：`接口名` 是 6 格，但 `上联口-核心` 是 11 格，所以列宽 11
- **`.rstrip()` 去掉行尾空格**：否则编辑器里会看到一堆尾随空格，git diff 很难看
- **`in_octets` 为 `None` 时打 `-`**：不要打 `0`，0 是「真的一个包都没收」

### 自检

```powershell
..\.venv\Scripts\python.exe ifwalk.py --port 1161
```

在终端里肉眼看：四行的「状态」是不是都在同一列。肉眼比逐字符数更可靠。

---

## 八、第 5 步：跑验收

```powershell
..\.venv\Scripts\python.exe check_d10.py
```

目标 **7/7**。脚本会自己起四台假设备（正常 / 缺状态 / 50 接口 / 打转），跑完自动关掉。

| 项 | 测什么 |
|---|---|
| C1 | 四个接口名都在，中文没变十六进制 |
| C2 | 恰好 4 行，没跳出 ifDescr 子树 |
| C3 | `max_rows=3` 就 3 行；打转设备 20 秒内必须结束 |
| C4 | `if_index` 三组，含四段行号那组 |
| C5 | 缺一行 status 时不错位，第 3 个是「未知」 |
| C6 | `down` 打出来了，列起点对齐 |
| C7 | `python ifwalk.py --port ...` 端到端 |

### 失败对照表

| 报错 | 原因 |
|---|---|
| C1 说「没有接口 '上联口-核心'，实际拿到 ['...','0xe4b88a...']」 | 用了 `prettyPrint()`。OctetString 要 `asOctets().decode("utf-8")` |
| C2 说「实际返回 20 行」 | 少了 `lexicographicMode=False` |
| C3 说「max_rows=3 应只返回 3 行，实际 50」 | 没在循环里数行数，或者 `break` 写在了内层 for 里 |
| C3 说「20 秒还没停下来」 | 有上限但没生效。检查 `len(rows)` 是加在内层还是外层循环 |
| C4 说「实际 [7, 12, 1]」 | `if_index` 写成了 `split(".")[-1]` |
| C5 说「应以 ifDescr 为准返回 4 行，实际 3」 | 用了 `zip`，长度取的是最短的那个 |
| C5 说「状态应是 ['up','up','未知','up']」 | 缺行时拿了下一行的值顶上，或者没给默认值「未知」 |
| C6 说「没有 down」 | 状态没做枚举映射，直接打了 `1`/`2` |
| C6 说「列起始位置不一致」 | 用了 `ljust` 而不是 `pad` |
| 整份验收卡住 / C5 超时 40 秒 | 把 `SnmpEngine()` 提到模块级复用了 —— 见第四节 ① |

### 如果看到这个

```
[警告] 127.0.0.1:xxxxx 1.3.6.1.2.1.2.2.1.2 已读到 200 行上限，停止 WALK
```

那说明 `lexicographicMode=False` 没生效，WALK 没停下来，是被你的 `max_rows` 兜住的。
上限救了你一命，但根因还是没修 —— C2 会红。

---

## 九、三个命令，看三种设备

跑完验收后自己玩一下，理解会比看文字深：

```powershell
# 1. 正常设备：4 个接口，第 3 个 down
..\.venv\Scripts\python.exe fake_agent.py --port 1161
..\.venv\Scripts\python.exe ifwalk.py --port 1161

# 2. 设备少回一行 status：第 3 个变「未知」，其余不错位
..\.venv\Scripts\python.exe fake_agent.py --port 1162 --miss-status
..\.venv\Scripts\python.exe ifwalk.py --port 1162

# 3. 50 个接口：只看前 5 行
..\.venv\Scripts\python.exe fake_agent.py --port 1163 --many
..\.venv\Scripts\python.exe ifwalk.py --port 1163 --max-rows 5
```

第 2 个和第 1 个对比，你能亲眼看到「按行号合并」和「zip」的差别。
第 3 个让你体会 `--max-rows` 在真实巡检里的用处：**一个 48 口交换机 WALK 完整张表要几十秒**，
巡检报告里通常只看前 N 行或者只看 down 的。

---

## 十、收尾

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35
git add week1/d10
git status
git commit -m "D10: WALK 接口表，walkCmd+lexicographicMode+按行号合并"
git pushall
```

`git status` 这一步要确认看到的是：`ifwalk.py`、`snmpio.py`、`health.py`、`fake_agent.py`、
`check_d10.py`、`README.md`，**没有** `out/`、`__pycache__`。

---

## 十一、今天学到的四件事（面试能讲的那种）

1. **WALK 不是一个协议操作，是一个循环**。GETNEXT 只保证「给你下一个更大的」，
   「哪里停」「走多深」全是客户端的事。`lexicographicMode` 就是「停在哪」。
2. **表是按列存的，不是按行**。所以「读一张表」= 读 N 列 × 每列 WALK 一次 + 客户端合并。
   合并的键是 OID 的行号段，不是数组下标。**用 `zip` 合并是错的**。
3. **行数上限是安全阀，不是优化**。真实设备会回不递增的 OID，没有上限的程序会永远转。
4. **中文数据在 SNMP 里是字节流**。`prettyPrint()` 遇到非 ASCII 会给十六进制，
   终端对齐又要按显示宽度算 —— 两个坑都在「字符串」这个概念上。

顺带一个版本陷阱：**pysnmp 6.x 的 `nextCmd` 不是 `walkCmd`**，网上大量教程是 4.x 时代的，
照抄会失败。这类「API 语义变了但函数名没变」的坑，读官方 changelog 比搜博客可靠。

---

## 十二、D11 预告

今天把表读出来了，但结果只在屏幕上一闪而过。D11 是**落盘**：
同一个结果同时写 CSV 和 JSON，CSV 用 `utf-8-sig`（否则 Excel 打开中文乱码）。

那时 `build_interfaces` 返回的 `list[Interface]` 会直接被复用 ——
这就是为什么今天要把它做成 dataclass，而不是直接打印。
