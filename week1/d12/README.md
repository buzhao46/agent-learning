# D12 · 错误分类：把「失败了」变成程序能处理的数据

**今天一句话**：D8 到今天，失败一律是一个字符串。今天要把它拆成**枚举 + 结构体 + 三态退出码**。

```
前 11 天                          今天之后
raise ProbeError("...超时...")    Failure(stage="sysDescr", kind=TIMEOUT,
     ↑                                    detail="No SNMP response...")
  上层只能 if "超时" in str(e)              ↑
                                    上层可以查、可以统计、可以决定重试不重试
```

预计 50–65 分钟。三个 TODO + 补一个 `return`，约 55 行。

---

## 一、先想清楚：为什么字符串不够

D8–D11 的写法是这一句：

```python
raise ProbeError(f"{host}:{port} {oid} 采集失败：{reason}")
```

**对人够用，对程序不够用。** 假设明天要写「超时的设备自动重试，认证错的设备直接告警」，你只有两种写法：

```python
if "超时" in str(exc):        # 改一个措辞，判定全崩
if "timeout" in str(exc):     # 版本一升级文案变了，同样崩
```

字符串是给人读的，随时可以改；判定逻辑不该建立在文案上。

更要命的是 D11 那种场景：一台设备 4 个项目采到 2 个，你现在没有任何办法把
「哪些成了、哪些没成、为什么没成」交给上层 —— 一个 `except ProbeError` 一刀切，
要么全当我成功，要么全当我失败。

今天做三件事：

| 要做的事 | 产物 | 为什么 |
|---|---|---|
| 把失败**分类** | `ErrorKind` 七种 | 不同类别对应不同的下一步动作 |
| 把失败**结构化** | `Failure(stage, kind, detail)` | 报告能按项说话，能统计、能进 JSON |
| 把「部分成功」变成一等公民 | `verdict` = OK / DEGRADED / FAILED → 退出码 0 / 2 / 1 | 五台设备坏一台，不该和五台全坏触发同一套动作 |

---

## 二、今天的目录

```
week1/d12/
├── probe.py         ← 你写，3 个 TODO（其余全部已写好）
├── check_d12.py     ← 7 项验收（不要改）
├── ifwalk.py        ← D10 的成品，原样搬来（walk 要用）
├── snmpio.py        ← D9 的，原样搬来（ProbeError / fmt_ticks）
├── fake_agent.py    ← D11 那台 + 三个新开关
└── README.md        ← 这份
```

**五个文件都已经放好了，不需要复制、不需要新建目录。**

`fake_agent.py` 这次多了三个开关，前 11 天那些开关都是「值不对」，这三个是「设备说不」：

| 开关 | 设备行为 | 客户端看到 |
|---|---|---|
| `--too-big` | ifDescr 的请求回 `error-status=tooBig(1)` | `error_status` = 1，varbind 是空值 |
| `--gen-err` | sysDescr 的请求回 `genErr(5)` | `error_status` = 5 |
| `--no-such-name` | sysName 的请求回 `noSuchName(2)` | `error_status` = 2 |

---

## 三、第 0 步：亲眼看见四件事（15 分钟，别跳过）

今天一半的判断力来自这一步。**这些现象你之前都没见过**，靠想象写不出判据。

### 0.1 起一台正常设备和一台「会说不」的设备

开两个 PowerShell 窗口，各自 `cd` 到 `d12`，**这两个窗口别关**：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d12

# 窗口 1
..\.venv\Scripts\python.exe fake_agent.py --port 1161

# 窗口 2
..\.venv\Scripts\python.exe fake_agent.py --port 1162 --gen-err --no-such-name
```

窗口 1 会打出来：

```
假设备已启动：127.0.0.1:1161  name=core-sw-01  community=public
接口数 4  miss_status=False  loop_bug=False  many=False
故障开关  too_big=False  gen_err=False  no_such_name=False
```

### 0.2 第一件事：community 错，不是「认证失败」，是超时

`probe.py` 还没写，先用一个 12 行的观察工具直接看协议层。把下面这段敲进去，
存成 `snoop.py`（**第 0 步的观察工具，不参与验收**，跑完留着就行）：

```python
"""把 getCmd 返回的四元组原样打出来 —— 看 pysnmp 到底给了我们什么。"""
import sys

from pysnmp.hlapi import CommunityData, ContextData, ObjectIdentity, ObjectType
from pysnmp.hlapi import SnmpEngine, UdpTransportTarget, getCmd

host, port, oid = sys.argv[1], int(sys.argv[2]), sys.argv[3]
community = sys.argv[4] if len(sys.argv) > 4 else "public"

ind, st, idx, vb = getCmd(
    SnmpEngine(),
    CommunityData(community),
    UdpTransportTarget((host, port), timeout=0.4, retries=0),
    ContextData(),
    ObjectType(ObjectIdentity(oid)),
)
print("indication :", type(ind).__name__, "|", ind)
print("status     :", type(st).__name__, "|", st, "| int =", int(st))
print("var_binds  :", len(vb), "个")
if vb:
    print("value      :", type(vb[0][1]).__name__, "|", vb[0][1].prettyPrint())
```

先用**正确**的 community 打一次：

```powershell
..\.venv\Scripts\python.exe snoop.py 127.0.0.1 1161 1.3.6.1.2.1.1.1.0
```

```
indication : NoneType | None
status     : int | 0 | int = 0
var_binds  : 1 个
value      : DisplayString | Huawei S5735-L48T4X-A1, VRP (R) software, Version 5.170
```

再用**错误**的 community 打同一台：

```powershell
..\.venv\Scripts\python.exe snoop.py 127.0.0.1 1161 1.3.6.1.2.1.1.1.0 wrong
```

```
indication : RequestTimedOut | No SNMP response received before timeout
status     : int | 0 | int = 0
var_binds  : 0 个
```

（`var_binds` 是空的，所以 `if vb:` 里面那行 `value` 不会打印。）

**看到没有：community 错了，设备连一个字节都没回。** 客户端拿到的
`RequestTimedOut` 和「网线拔了」「防火墙拦了 161/udp」完全一模一样。

> **这就是今天最重要的一条：SNMPv2c 没有「认证失败」这个错误。**
>
> 协议里根本没有让设备回「你 community 错了」的字段 —— v2c 的认证就是
> 「community 对就应答，不对就把包丢掉」，一个字节都不给。
> （SNMPv3 才有 `usmStatsWrongDigests` 这类计数器，那是另一套东西。）
>
> 所以今天写的 `ErrorKind` 里**没有** AUTH 这一项。不是偷懒，是协议不许你分。
> 分类里硬编一个 `AUTH`，等于告诉上层「我能分清网络故障和密码错」—— 你分不清。

等 TODO 2 写完，把同一个动作换成你的代码再看一次：

```powershell
..\.venv\Scripts\python.exe probe.py --port 1161 --community wrong
```

同一件事，从「一坨字符串」变成了「4 条结构化失败 + 一个退出码 1」——
这就是今天全部的价值。

### 0.3 第二件事：三层的类型完全不同

拿窗口 2 那台「会说不」的设备，逐个打三个 OID：

```powershell
# 设备说不认识（noSuchName，error-status=2）
..\.venv\Scripts\python.exe snoop.py 127.0.0.1 1162 1.3.6.1.2.1.1.5.0
```

```
indication : NoneType | None
status     : Integer | noSuchName | int = 2
var_binds  : 1 个
value      : Null |              ← 注意：是个空值（Null），不是「没这一行」
```

（`Integer | noSuchName` —— `prettyPrint()` 给的是名字，`int()` 才是数字。
两个都要，因为 19 个错误码只有 5 个有常用的名字。）

```powershell
# 设备自己出错（genErr，error-status=5）
..\.venv\Scripts\python.exe snoop.py 127.0.0.1 1162 1.3.6.1.2.1.1.1.0
```

```
indication : NoneType | None
status     : Integer | genErr | int = 5
var_binds  : 1 个
value      : Null |
```

```powershell
# 设备正常应答，但这个 OID 不存在（NoSuchObject —— 是「值」不是「状态」）
..\.venv\Scripts\python.exe snoop.py 127.0.0.1 1161 1.3.6.1.2.1.1.99.0
```

```
indication : NoneType | None
status     : int | 0 | int = 0        ← 状态是 0，没报错！
var_binds  : 1 个
value      : NoSuchObject | No Such Object currently exists at this OID
```

把几次的结果并排看，今天要写的判据就出来了：

| 层 | pysnmp 的字段 | 类型 | 典型值 | 归到哪个 kind |
|---|---|---|---|---|
| 传输层 | `error_indication` | 异常**对象**（被 `return` 出来，不是 `raise`） | `RequestTimedOut()` | `TIMEOUT` |
| PDU 层 | `error_status` | **整数** | `1` / `2` / `5` | `TOO_BIG` / `BAD_OID` / `DEVICE_ERROR` |
| PDU 层 | 同上，`prettyPrint()` | 名字 | `tooBig(1)` | 同上 |
| 值层 | varbind 的 value | `NoSuchObject` / `NoSuchInstance` **对象** | `NoSuchObject()` | `NO_DATA` |

三层的**类型**各是一个世界：异常对象、整数、伪装成值的对象。
所以你不可能用一套 `if` 判完，也不该用字符串去猜。

### 0.4 第三件事：`status` 的类型会变

对比 A 和 C 两块输出：

```
A  status : int       | 0            | int = 0      ← 没出错时，是原生 int
C  status : Integer   | noSuchName   | int = 2      ← 出错时，是 pysnmp 的 Integer 对象
```

同一个字段，没出错时是 `int`，出错时是 `Integer`。这意味着：

- **不能**无条件 `status.prettyPrint()` —— 原生 `int` 没有这个方法，会 `AttributeError`
- **不能**用 `if status:` 判有没有错 —— 虽然 `int(0)` 是假、`Integer(2)` 是真，
  但读代码的人不知道你在判什么
- **只能** `int(status) != 0`。这就是为什么 `classify` 和 `get_raw` 里到处是 `int(status)`

顺便：`value` 那行打出来是 `DisplayString` 而不是 `OctetString`。`DisplayString` 是
`OctetString` 的子类，所以 `isinstance(value, OctetString)` 仍然是 `True` —— 你的
取值代码不用改，但看输出时别以为自己拿错了类型。

### 0.5 第四件事：`retries=0` 不传，一次超时变 3.6 秒

```powershell
..\.venv\Scripts\python.exe -c "import time;from pysnmp.hlapi import *;t0=time.perf_counter();getCmd(SnmpEngine(),CommunityData('bad'),UdpTransportTarget(('127.0.0.1',1161),timeout=0.4,retries=0),ContextData(),ObjectType(ObjectIdentity('1.3.6.1.2.1.1.1.0')));print(f'retries=0 -> {time.perf_counter()-t0:.2f}s')"
```

把这条命令里的 `retries=0` 改成 `retries=5` 再跑一次。实测：

```
retries=0 -> 1.11s
retries=5 -> 3.62s
```

`timeout=0.4` 却花了 1.11s —— 多出来的 0.7s 是 pysnmp 每次 `getCmd` 的固定开销
（新建 asyncio dispatcher）。**这 0.7s 和 `timeout` 无关，所以不要用绝对耗时去
反推 timeout 有没有生效。**

记住这个数：**`retries=0` → 约 1.1s；不传（默认重试 5 次）→ 约 3.6s。**
验收 C3 用的就是这两个数之间的阈值（2.2s）。D14 做并发扫描时，这 0.7s ×
设备数 × 项目数会变成主要耗时 —— 到时候要处理。

---

## 四、TODO 1：`classify` —— 三层字段 → 一个类别

先看骨架里已经写好的 `ErrorKind`，七个类别，每个带一个 `hint`：

```powershell
..\.venv\Scripts\python.exe -c "import probe; [print(f'{k.value:<14} {k.hint[:40]}…') for k in probe.ErrorKind]"
```

```
timeout        设备没应答。按这个顺序排查：community 对不对 → ping 通不通 →…
unreachable    地址或端口不对：DNS 解析失败、UDP 端口不可达、或设备没起 SNMP 服务…
bad_oid        设备不认识这个 OID（noSuchName）。MIB 不支持该节点，或者 OI…
too_big        应答超过设备能发的最大报文（tooBig）。一次少请求几个 OID，别硬塞。…
device_error   设备自己出错了（genErr / noAccess / resourceUnav…
no_data        OID 存在，但这一格没有值（NoSuchObject / NoSuchInst…
protocol       报文层面没对上：版本不匹配、对端不是 SNMP 服务、或解析异常。…
```

看一遍就知道今天的设计意图：**每个类别必须对应一个下一步动作**。
「超时」不是信息，「超时：先查 community，再 ping，最后查 ACL」才是。

### 4.1 分四步写

`classify` 一共 4 个分支，**按这个顺序写，每写完一个分支就跑一次自检**。

**第 1 步：先写空壳，确认能 import**

在 `probe.py` 第 209 行，把 `raise NotImplementedError(...)` 换成：

```python
    if indication is not None:
        pass
    if status is not None and int(status) != 0:
        pass
    if isinstance(value, (rfc1905.NoSuchObject, rfc1905.NoSuchInstance)):
        pass
    return None
```

自检（这一步只要求不报语法错）：

```powershell
..\.venv\Scripts\python.exe -c "import probe; print(probe.classify(None, None, None))"
```

```
None
```

**第 2 步：填 indication 分支（3 个判断）**

把第一个 `pass` 换成：

```python
    if indication is not None:
        if isinstance(indication, errind.RequestTimedOut):
            return ErrorKind.TIMEOUT
        if isinstance(indication, errind.EmptyResponse):
            return ErrorKind.PROTOCOL
        return ErrorKind.UNREACHABLE
```

自检：

```powershell
..\.venv\Scripts\python.exe -c "from pysnmp.proto import errind; import probe; print(probe.classify(errind.RequestTimedOut(), None, None)); print(probe.classify(errind.EmptyResponse(), None, None)); print(probe.classify(errind.OtherError('x'), None, None))"
```

```
ErrorKind.TIMEOUT
ErrorKind.PROTOCOL
ErrorKind.UNREACHABLE
```

> 为什么 `EmptyResponse` 要单独拎出来？它是「包收到了、里面是空的」——
> 协议层问题；`RequestTimedOut` 是「一个字节都没收到」。漏掉这条，
> 一台被端口扫描器碰过的设备会被判成「端口不可达」，你会去查网络，
> 而真正的问题是设备回了空包。

**第 3 步：填 status 分支（三个数字）**

```python
    if status is not None and int(status) != 0:
        code = int(status)
        if code == 1:
            return ErrorKind.TOO_BIG
        if code == 2:
            return ErrorKind.BAD_OID
        return ErrorKind.DEVICE_ERROR
```

自检：

```powershell
..\.venv\Scripts\python.exe -c "from pysnmp.proto import rfc1905; import probe; print([probe.classify(None, rfc1905.errorStatus.clone(c), None) for c in (1,2,5,6)])"
```

```
[<ErrorKind.TOO_BIG: 'too_big'>, <ErrorKind.BAD_OID: 'bad_oid'>,
 <ErrorKind.DEVICE_ERROR: 'device_error'>, <ErrorKind.DEVICE_ERROR: 'device_error'>]
```

注意 1 和 2 有专名，其它（5 genErr、6 noAccess、13 resourceUnavailable…）
全部落到 `DEVICE_ERROR`。不要给 19 个错误码各配一个类别 —— 分类是为了定动作，
这 19 个码的下一步动作是同一件事：**看设备日志**。

**第 4 步：填 value 分支**

```python
    if isinstance(value, (rfc1905.NoSuchObject, rfc1905.NoSuchInstance)):
        return ErrorKind.NO_DATA

    return None
```

自检：

```powershell
..\.venv\Scripts\python.exe -c "from pysnmp.proto import rfc1905; import probe; print(probe.classify(None, rfc1905.errorStatus.clone(0), rfc1905.NoSuchObject()))"
```

```
ErrorKind.NO_DATA
```

### 4.2 一条硬规矩

骨架的 docstring 里写了，这里再说一遍：

```python
if "超时" in str(indication):      # ← 永远不要
```

实测 `str(errind.RequestTimedOut())` 是 `"No SNMP response received before timeout"`。
里面既没有「超时」也不是你想的那个词。**按类型判，永远不会因为文案改动而失效。**

---

## 五、TODO 2：`get_raw` —— 发一次 GET，永不抛异常

`probe.py` 第 262 行。这个函数是今天的设计决策所在：**用返回值而不是异常**。

为什么？

> 调用方要在循环里采 4 个项目，任何一个失败都不能中断后面的。
> 用返回值，失败的证据（`Failure`）**必须被塞进某个筐里**；
> 用异常，一句宽容的 `except Exception: pass` 就能让整项悄悄消失，
> 而报告看起来还是完整的。

### 5.1 分五步

**第 1 步：先只写 try + getCmd，返回两个 None**

```python
    try:
        indication, status, _index, var_binds = getCmd(
            SnmpEngine(),
            CommunityData(community),
            UdpTransportTarget((host, port), timeout=timeout, retries=0),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
    except Exception:
        return None, None
    return None, None
```

自检（不报错就行）：

```powershell
..\.venv\Scripts\python.exe -c "import probe; print(probe.get_raw('127.0.0.1', probe.OID_SYSDESCR, port=1161, timeout=0.4))"
```

```
(None, None)
```

**第 2 步：填 except 分支**

```python
    except Exception as exc:
        return None, Failure(
            stage=oid, kind=ErrorKind.PROTOCOL, detail=f"{type(exc).__name__}: {exc}"
        )
```

`try` 挡住的是**真的**异常（socket 层炸了、DNS 解析器抛错）。这类异常在四元组里
没有位置，只能靠 `except` 兜住。

**第 3 步：取 value，交给 classify**

```python
    value = var_binds[0][1] if var_binds else None
    kind = classify(indication, status, value)
```

`var_binds` 可能是空的（超时时就是空的），所以要先判空再取 `[0][1]`。

**第 4 步：错误分支，组装 Failure**

```python
    if kind is not None:
        if indication is not None:
            detail = str(indication)
        elif status is not None and int(status) != 0:
            detail = f"{status.prettyPrint()}({int(status)})"
        elif value is not None:
            detail = type(value).__name__
        else:
            detail = "空响应：var_binds 是空的"
        return None, Failure(stage=oid, kind=kind, detail=detail)
```

**四个 `detail` 的来源必须分清楚**，这是报告能不能定位问题的关键：

| 情况 | detail 长什么样 |
|---|---|
| 超时 | `No SNMP response received before timeout` |
| 设备拒绝 | `genErr(5)` / `tooBig(1)` |
| 值层缺数据 | `NoSuchInstance` |
| 空响应 | `空响应：var_binds 是空的` |

**第 5 步：成功分支，转文本**

```python
    if isinstance(value, OctetString):
        return value.asOctets().decode("utf-8", "replace"), None
    return value.prettyPrint(), None
```

和 D10 的 `walk` 里同一个道理：`OctetString` 用 `prettyPrint()` 会把中文打成
十六进制，必须走 `asOctets().decode()`。

### 5.2 自检：三种情况一起看

```powershell
..\.venv\Scripts\python.exe -c "import probe; print('正常  ', probe.get_raw('127.0.0.1', probe.OID_SYSDESCR, port=1161, timeout=0.4)); print('错社区', probe.get_raw('127.0.0.1', probe.OID_SYSDESCR, port=1161, community='bad', timeout=0.4)); print('genErr', probe.get_raw('127.0.0.1', probe.OID_SYSDESCR, port=1162, timeout=0.4))"
```

期望（第二行注意 kind 是 `TIMEOUT`）：

```
正常   ('Huawei S5735-L48T4X-A1, VRP (R) software, Version 5.170', None)
错社区 (None, Failure(stage='1.3.6.1.2.1.1.1.0', kind=<ErrorKind.TIMEOUT: 'timeout'>, detail='No SNMP response received before timeout'))
genErr (None, Failure(stage='1.3.6.1.2.1.1.1.0', kind=<ErrorKind.DEVICE_ERROR: 'device_error'>, detail='genErr(5)'))
```

---

## 六、TODO 3：`collect_device` —— 4 个项目，谁坏都不影响别人

`probe.py` 第 313 行。这一步逻辑最简单，但**有一条不变量必须守住**：

```
len(values) + len(failures) == 4    永远成立
```

失败的项目不是「从结果里消失」，而是「换一个筐」。这条不变量是证明
「漏采」和「采集失败」没被混淆的唯一手段 —— 验收 C4/C5 就查它。

### 6.1 分三步

**第 1 步：建 result + 循环采三个 GET**

```python
    result = CollectionResult(host=host)

    for stage, oid in (
        ("sysDescr", OID_SYSDESCR),
        ("uptime", OID_SYSUPTIME),
        ("sysName", OID_SYSNAME),
    ):
        text, failure = get_raw(host, oid, community=community, port=port, timeout=timeout)
        if failure is None:
            result.values[stage] = text
        else:
            result.failures.append(
                Failure(stage=stage, kind=failure.kind, detail=failure.detail)
            )
```

第 10 行那个 `Failure(...)` 是**重新造一个**，不是直接 `append(failure)`。
因为 `get_raw` 返回的 `stage` 是 **OID 字符串**，而报告要的是**项目名**。
运维记不住 `1.3.6.1.2.1.1.1.0`。

**第 2 步：单独采 ifTable（这是 WALK，不是 GET）**

```python
    try:
        rows = walk(host, OID_IF_DESCR, community=community, port=port, timeout=timeout)
        result.values["ifTable"] = len(rows)
    except ProbeError as exc:
        result.failures.append(
            Failure(stage="ifTable", kind=ErrorKind.PROTOCOL, detail=str(exc))
        )

    return result
```

存的是**接口个数**（`len(rows)`），不是整张表 —— 今天的重点是分类，不是数据。

**第 3 步：想清楚这条为什么只能归 `PROTOCOL`**

`walk` 是 D10 写的，它在出错时 `raise ProbeError(f"...采集失败：{reason}")`——
`error_status` 已经被压成一句话了，根因丢了。所以这里没法知道是 tooBig 还是超时。

**这是今天唯一一处将就。** 在注释里承认它，比假装分类很精确要好：

```python
        # D10 的 walk 只吐字符串，根因已经丢了，只能归 PROTOCOL。
        # D14 合并时会把它也改成结构化返回。
```

### 6.2 别短路

「第一项就超时了，后面三项不用试了吧」——**错**。

四项的失败原因可能不同（sysDescr 超时、ifTable 却正常）。短路之后报告里只有
一条失败，运维会以为整台设备掉线，直接走升级流程。你省下 3 次超时（约 3 秒），
代价是把诊断信息砍掉 3/4。

---

## 七、跑验收

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d12
..\.venv\Scripts\python.exe check_d12.py
```

验收脚本会自己拉起三台假设备（正常 / 两个错误码 / tooBig），跑完自动关掉。
**你窗口里那两台不用关，不冲突**（端口是随机分配的）。

七项在查什么：

| 项 | 查什么 | 不通过时最常见的原因 |
|---|---|---|
| C1 | 8 组输入 → 8 个类别 | 判据顺序错 / 「都正常」没返回 None |
| C2 | 成功路径拿到值 | 三个字段都正常时 `classify` 还返回了某个 kind |
| C3 | 失败**不抛异常** + kind=TIMEOUT + 耗时 < 2.2s | 忘了 `retries=0`（会到 3.6s） |
| C4 | community 错 → 3 个 GET 项全 timeout，verdict=FAILED，退出码 1 | 分类里编了 `AUTH` 之类不存在的类别 |
| C5 | genErr + noSuchName 分类正确，另两项照常采到 | 失败时 `continue` 掉，没 append |
| C6 | tooBig 只拖垮 ifTable 一列 | `walk` 没包 `try`，整体崩了 |
| C7 | `python probe.py --port ...` 退出码 0 | 加练，顺手看输出格式 |

全绿之后，看报告长什么样（**这才是今天的成果**）：

```powershell
..\.venv\Scripts\python.exe probe.py --port 1161
```

```
127.0.0.1        OK        4/4 项成功
    sysDescr  Huawei S5735-L48T4X-A1, VRP (R) software, Version 5.170
    uptime    1 day, 10:17:36.78
    sysName   core-sw-01
    ifTable   4
```

```powershell
..\.venv\Scripts\python.exe probe.py --port 1162
```

```
127.0.0.1        DEGRADED  2/4 项成功
    uptime    1 day, 10:17:36.78
    ifTable   4
    [!] sysDescr: device_error（genErr(5)）
      → 设备自己出错了（genErr / noAccess / resourceUnavailable …）。查设备日志和 ACL 配置，重试通常没用。
    [!] sysName: bad_oid（noSuchName(2)）
      → 设备不认识这个 OID（noSuchName）。MIB 不支持该节点，或者 OID 拼错了。
```

```powershell
..\.venv\Scripts\python.exe probe.py --port 1161 --community wrong
echo "退出码 $LASTEXITCODE"
```

```
127.0.0.1        FAILED    0/4 项成功
    [!] sysDescr: timeout（No SNMP response received before timeout）
      → 设备没应答。按这个顺序排查：community 对不对 → ping 通不通 → 161/udp 有没有被 ACL 或防火墙拦。…
    ...
退出码 1
```

**一份报告同时回答了「坏在哪」和「下一步干什么」—— 这是 D8 到今天所有工作的兑现。**

---

## 八、卡住了怎么办

| 报错 / 现象 | 原因 | 怎么改 |
|---|---|---|
| `TypeError: cannot unpack non-sequence NoneType` | `get_raw` 某个分支忘了 `return`，函数隐式返回 None | 每个分支都写 `return xxx, yyy` |
| C1 报「都正常」那组不对 | 最后落了 `DEVICE_ERROR` 兜底 | 三个字段都空时**必须** `return None` |
| C2 拿到的是 `0xe4b88ae8...` | 走了 `prettyPrint()` | `OctetString` 走 `asOctets().decode("utf-8", "replace")` |
| C3 报「花了 3.6s」 | `UdpTransportTarget` 没传 `retries=0` | 加上，见 0.4 的实测 |
| C3 报「get_raw 抛异常了」 | 用了 `raise ProbeError(...)` | 改成 `return None, Failure(...)` |
| C4 报「出现了 ['auth'] 这一类」 | 分类里自己编了协议没有的类别 | 删掉。v2c 认证失败 = 超时，见 0.2 |
| C4 报 `total` 不是 4 | 某个分支 `continue` 掉了，没 append | 失败也要进 `failures` |
| C5 报「uptime 不在 values 里」 | 前一项失败后 `break` 了 | 不短路，4 项都走一遍 |
| C6 报「ifTable 没被记成失败」 | `walk` 没包 `try` | 见 6.1 第 2 步 |
| 假设备一直起不来 | 端口被上次的进程占着 | 换端口，或任务管理器杀掉 python |

---

## 九、今天必须记住的三条

**1. SNMPv2c 没有「认证失败」这个错误。**
community 错 = 设备静默丢包 = 客户端看到超时。任何声称能区分「密码错」和
「网络断」的代码，都在骗自己。所以 `TIMEOUT` 的 `hint` 必须把三种可能都列出来。

**2. 分类的产出必须是动作，不是名词。**
`ErrorKind` 里的 `hint` 比 `ErrorKind` 本身更重要。「超时」不构成信息，
「超时：先查 community → ping → 查 ACL」才是运维能用的东西。

**3. 部分成功要独立的三态。**
OK / DEGRADED / FAILED → 0 / 2 / 1。「一半坏了」和「全坏了」在自动化里必须
能触发不同动作，否则你把人工判断塞进脚本的那一刻就埋了一个坑。

顺带一个工程习惯：`len(values) + len(failures) == 4` 这种**不变量**，
写进注释、写进验收，比写十行说明文档管用。

---

## 十、明天（D13）预告

今天是「一台设备采得准」，明天是「一批设备扫得动」：把 host 清单、community、
超时从命令行参数搬进 `devices.json`，让 `probe.py` 读配置扫一整批。

今天的 `CollectionResult` 就是为明天准备的 —— 到时候是 `list[CollectionResult]`，
退出码取最坏的那个。**不用改今天写的任何一行**。
