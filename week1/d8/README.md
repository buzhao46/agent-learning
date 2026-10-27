# D8 · 第一次真采集（pysnmp getCmd）

> 21 天训练 · 第 2 周「接真数据」第一天
> 预计 50 分钟 · 你要写 3 个函数，约 40 行

---

## 一、今天和前几天有什么不同

D1–D7 你操作的全是**自己电脑上的 json 文件**。今天开始，程序要真的发出一个 UDP 包，去问一台网络设备「你是谁」，然后把回包解开。

这是「脚本」和「工具」的分界线。也是你面试时最值钱的一段经历——因为真设备**会超时、会拒绝、会返回莫名其妙的东西**，处理这些的经验买不到。

三天的分工：

| 天 | 干什么 |
|---|---|
| **D8（今天）** | 用 `getCmd` 读回三个标量，把错误路径拦住 |
| D9 | 把结果转成 dataclass，处理异常值（`safe_int`） |
| D10 | 用 `nextCmd` 走 WALK，读接口表 |

今天只读三样东西：设备型号、设备名、开机时长。**不 WALK、不落盘、不做配置外置**，那些是后面的事。

---

## 二、环境：pysnmp 已经装好了

我把它装进了 `week1/.venv`（不是系统 Python）。先确认一遍：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1
.venv\Scripts\python.exe -c "import pysnmp; print(pysnmp.__version__)"
```

要打印 `6.1.4`。

> 如果你激活了 venv（前面有 `(.venv)`），直接 `python -c "..."` 也能行。没激活就用上面那串完整路径。

### 你没有交换机，所以给你配了一台「假设备」

`fake_agent.py` 是我写的一个 80 行的小程序：它监听 `127.0.0.1:1161`，收到 SNMP GET 就用标准 BER 编码回包。

**对 pysnmp 来说，它和一台真的华为 S5735 没有区别**——同样的报文格式、同样的超时、同样的 `noSuchObject`。所以你今天踩到的坑，全是真的坑。

它应答这几个 OID：

| OID | 名字 | 回什么 |
|---|---|---|
| `1.3.6.1.2.1.1.1.0` | sysDescr | `Huawei S5735-L48T4X-A1, VRP (R) software, Version 5.170` |
| `1.3.6.1.2.1.1.5.0` | sysName | `core-sw-01` |
| `1.3.6.1.2.1.1.3.0` | sysUpTime | `12345678`（TimeTicks） |
| 其它任何 OID | — | `noSuchObject` |

**你自己开一次看看**（另开一个 PowerShell 窗口，别关）：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d8
..\.venv\Scripts\python.exe fake_agent.py
```

看到 `假设备已启动：127.0.0.1:1161` 就成了。**这个窗口要一直开着**，回到原来那个窗口继续。

> `check_d8.py` 会给自己另开一个端口、自己拉起假设备、跑完自己关掉，**不依赖你手动开的这个**。所以你开着的窗口不影响验收，只是方便你手动试。

---

## 三、第 0 步（5 分钟）：先看清楚返回值长什么样

**不要跳过这一步。** 今天一半的坑都在这个返回值上。

开一个临时文件，或者直接用 `-c` 跑下面这段（在 `d8` 目录下，假设备开着）：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d8
..\.venv\Scripts\python.exe -c "
from pysnmp.hlapi import *
eng = SnmpEngine()
r = getCmd(eng, CommunityData('public'), UdpTransportTarget(('127.0.0.1', 1161), timeout=1.0, retries=0), ContextData(), ObjectType(ObjectIdentity('1.3.6.1.2.1.1.1.0')))
print('整体类型:', type(r))
print('有', len(r), '个元素')
for i, x in enumerate(r):
    print(f'  [{i}]', type(x).__name__, '=', x)
"
```

你会看到四个东西：

```
[0] NoneType = None                    ← error_indication，网络层有没有失败
[1] Integer32 = 0                       ← error_status，设备有没有拒绝
[2] Integer32 = 0                       ← error_index，第几个 varbind 出的问题
[3] list = [ObjectType(...)]            ← var_binds，真正的数据
```

**四个都要看。** 新手最常犯的错是只看第一个：`error_indication` 是 `None` 就以为成功了。

再把第四个拆开看一层：

```powershell
..\.venv\Scripts\python.exe -c "
from pysnmp.hlapi import *
eng = SnmpEngine()
_, _, _, vbs = getCmd(eng, CommunityData('public'), UdpTransportTarget(('127.0.0.1', 1161), timeout=1.0, retries=0), ContextData(), ObjectType(ObjectIdentity('1.3.6.1.2.1.1.1.0')))
name, value = vbs[0]
print('OID =', name)
print('值的类型 =', type(value).__name__)
print('prettyPrint() =', repr(value.prettyPrint()))
"
```

`var_binds` 的形状是 `[(OID, 值), ...]`，即使你只查一个 OID 也是一个长度为 1 的列表。**所以永远要遍历或取 `[0]`，不能直接把 `vbs` 当值用。**

> 这一步的代码不用保存，看完就够。**要看懂再往下写。**

---

## 四、第 1 步（20 分钟）：`snmp_get`（第 91 行）

打开 `getprobe.py`，第 91 行是 `raise NotImplementedError("snmp_get 还没写")`，把它换成真正的实现。

### 骨架

```python
def snmp_get(host, oid, *, community="public", port=161, timeout=1.0) -> str:
    # ① 发请求
    try:
        error_indication, error_status, error_index, var_binds = getCmd(
            _ENGINE,
            CommunityData(community),
            UdpTransportTarget((host, port), timeout=timeout, retries=0),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
    except Exception as exc:
        raise ProbeError(f"{host}:{port} {oid} 请求异常：{exc}") from exc

    # ② 三道门，一个都不能跳
    if error_indication:
        raise ProbeError(...)      # 网络层失败
    if error_status:
        raise ProbeError(...)      # 设备拒绝
    if not var_binds:
        raise ProbeError(...)      # 空响应

    # ③ 取值
    _, value = var_binds[0]
    if <值是 NoSuchObject / NoSuchInstance>:
        raise ProbeError(...)
    return str(value.prettyPrint())
```

### 三个必须想清楚的点

**① `retries=0` 不能省**

`UdpTransportTarget` 默认 `retries=5`，意思是一个包最多重发 5 次。你传 `timeout=0.5`，它会老老实实等 `0.5 × (5+1) = 3 秒` 才放弃。

本机测试几乎看不出来，因为假设备秒回。但**一旦设备真的挂了，巡检 20 台就要多等 60 秒**。C5 会卡这一条：`timeout=0.5` 却花了 3 秒以上就报错。

**② 异常消息里必须带 `host` 和 `port`**

`raise ProbeError("采集失败")` 是一句废话。巡检报告里 20 台设备，你得知道是哪台。

写成 `f"{host}:{port} {oid} 采集失败：{error_indication}"`，一条日志就能定位。C5 会检查消息里同时含 host 和端口号。

**③ 不存在的 OID，`error_status` 是 0**

这是今天最隐蔽的坑，单独说一节。

### ★ 最难的一点：`NoSuchObject`

你查一个设备上不存在的 OID，会发生什么？

直觉是「设备报错」。实际上 SNMPv2c 的规范是：`error_status = 0`（没报错），但 varbind 里的值不是字符串，而是一个特殊标记 `NoSuchObject`。

它的 `prettyPrint()` 返回：

```
No Such Object currently exists at this OID
```

**这是一句英文错误信息，但它长得非常像一个正常的值。** 如果你直接 `return value.prettyPrint()`，就会把这句话当成设备型号存进数据库、写进报告、发给客户。

判据是**类型**，不是内容：

```python
from pysnmp.proto.rfc1905 import NoSuchInstance, NoSuchObject

if isinstance(value, (NoSuchObject, NoSuchInstance)):
    raise ProbeError(f"{host}:{port} {oid} 该 OID 在设备上不存在")
```

加在 `from pysnmp.hlapi import (...)` 那段后面即可。

> 偷懒的写法是 `type(value).__name__ in ("NoSuchObject", "NoSuchInstance")`，也能过 C6，但 `isinstance` 更稳——真实设备还会返回 `NoSuchInstance`、`EndOfMibView` 等变体。

### 一个顺手记下的小坑：`error_status` 不一定是对象

第 0 步你看到 `[1] int = 0` —— 它是**普通 int**，不是 `Integer32`。

所以这行会在真机报错时炸掉：

```python
raise ProbeError(error_status.prettyPrint())   # ❌ AttributeError: 'int' object has no attribute 'prettyPrint'
```

本机测试永远碰不到，因为假设备总回 0（永远走不到这个分支）。**真机一旦返回 `genErr` / `tooBig`，你就收获一个 `AttributeError`，而且它把真正的错误码盖掉了。**

一律用 `str(error_status)` 或直接放进 f-string。**「只在错误路径上才会执行的代码」，永远要用最保守的写法**——因为它平时根本没被执行过，没人知道它会不会崩。


### 自检

假设备开着的前提下：

```powershell
..\.venv\Scripts\python.exe -c "
import getprobe as g
print(g.snmp_get('127.0.0.1', g.OID_SYSDESCR, port=1161))
"
```

要打印 `Huawei S5735-L48T4X-A1, VRP (R) software, Version 5.170`。

再试错的两条路：

```powershell
..\.venv\Scripts\python.exe -c "
import getprobe as g
g.snmp_get('127.0.0.1', g.OID_SYSDESCR, port=19999, timeout=0.5)
"
```

要看到 `ProbeError: 127.0.0.1:19999 1.3.6.1.2.1.1.1.0 采集失败：No SNMP response received before timeout`，而且**立刻**返回（不是等 3 秒）。

```powershell
..\.venv\Scripts\python.exe -c "
import getprobe as g
g.snmp_get('127.0.0.1', '1.3.6.1.2.1.1.9.0', port=1161)
"
```

要看到 `ProbeError: ... 该 OID 在设备上不存在`。**如果它打印出了那句英文，说明 `NoSuchObject` 没判。**

---

## 五、第 2 步（10 分钟）：`fmt_ticks`（第 109 行）

纯函数，不碰网络，最好改。**先做这个也行**——如果 `snmp_get` 卡住了，跳过来做它，能拿一分。

### 单位是坑

`sysUpTime` 的单位**不是秒**，是**百分之一秒**（TimeTicks）。

```
12345678 → 123456.78 秒 → 约 34 小时
```

忘掉 `/100`，你会得到 `142 day`（实际是 1 天 10 小时）。**这个错看起来完全正常**，没人会怀疑一台交换机开了 142 天。故障注入时我看到的正是 `142 day, 21:21:18.00`。

### 约定的输出格式

```
12345678  →  "1 day, 10:17:36.78"
6000      →  "00:01:00.00"
```

规则：

- 秒保留两位小数
- 有整天时：`{天数} day, {时:02d}:{分:02d}:{秒:05.2f}`
- 不足一天时：不写天数，直接 `{时:02d}:{分:02d}:{秒:05.2f}`

`check_d8.py` 会**逐字符**比对这两个例子，所以格式必须完全一致。

### 骨架

```python
def fmt_ticks(ticks: int) -> str:
    total = ticks / 100          # ← 这一行是今天的重点
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    clock = f"{int(hours):02d}:{int(minutes):02d}:{seconds:05.2f}"
    return f"{int(days)} day, {clock}" if days else clock
```

`divmod(a, b)` 返回 `(商, 余数)`，一次拿到两个值，比写 `a // b` 和 `a % b` 少写一行。后面拆「天→时→分→秒」用它很顺。

> 注意 `divmod` 出来的是 float（因为 `total` 是 float），所以格式化时 `int(hours)`、`int(minutes)`；`seconds` 保留 float，才能显示两位小数。

### 自检

```powershell
..\.venv\Scripts\python.exe -c "
import getprobe as g
print(repr(g.fmt_ticks(12345678)))
print(repr(g.fmt_ticks(6000)))
print(repr(g.fmt_ticks(0)))
"
```

期望：

```
'1 day, 10:17:36.78'
'00:01:00.00'
'00:00:00.00'
```

---

## 六、第 3 步（5 分钟）：`probe_health`（第 128 行）

到这一步 `snmp_get` 已经能用了，这里只是**组装**。

```python
def probe_health(host, *, community="public", port=161, timeout=1.0) -> dict[str, str]:
    def get(oid: str) -> str:
        return snmp_get(host, oid, community=community, port=port, timeout=timeout)

    return {
        "sys_descr": get(OID_SYSDESCR),
        "sys_name": get(OID_SYSNAME),
        "uptime": fmt_ticks(int(get(OID_SYSUPTIME))),
    }
```

两个细节：

- 里面套一个小函数 `get`，是为了不用把 `community=..., port=..., timeout=...` 抄三遍。**三个参数抄三遍，改一个忘两个是迟早的事。**
- `int(...)` 不能省：`snmp_get` 返回的是字符串（`prettyPrint()` 的产物），`fmt_ticks` 要的是整数。

**不要在这里 `try/except`。** 让 `ProbeError` 直接往上抛——是否重试、是否跳过这台、是否计入告警，是上层（D12）的事。**下层吞掉异常，上层就永远做不出正确决策。**

### 自检

```powershell
..\.venv\Scripts\python.exe getprobe.py --host 127.0.0.1 --port 1161
```

期望三行：

```
设备 127.0.0.1:1161
  型号  Huawei S5735-L48T4X-A1, VRP (R) software, Version 5.170
  名称  core-sw-01
  运行  1 day, 10:17:36.78
```

---

## 七、第 4 步（5 分钟）：跑验收

```powershell
..\.venv\Scripts\python.exe check_d8.py
```

目标 **7/7**。它会自己开端口、自己拉起假设备、跑完自己关掉，你不用管你那个窗口。

### 失败对照表

| 报错 | 原因 |
|---|---|
| C1 `没读回 sysDescr` | `getCmd` 四个返回值没解开，或者你把 `(OID, 值)` 整个元组返回了 |
| C1 `抛了 ProbeError: ... No SNMP response` | `port` 参数没接进 `UdpTransportTarget`，写死成 161 了 |
| C2 `sysName 应为 'core-sw-01'，实际 'Huawei...'` | OID 写死在 `snmp_get` 里了，没接参数 |
| C3 `142 day, 21:21:18.00` | 忘了 `/100`，把 ticks 当秒了 |
| C3 `1 day, 10:17:36.78` 只错第二个 | 不足一天时不该写 `0 day, ` |
| C4 `缺键 'uptime'` | 返回的字典键名不对，必须是 `sys_descr` / `sys_name` / `uptime` |
| C4 `uptime` 是 `12345678` | 忘了调 `fmt_ticks` |
| C5 `居然「成功」返回了 ''` | 超时的时候没检查 `error_indication` |
| C5 `抛的是 RuntimeError` | 抛了内置异常，不是 `ProbeError` |
| C5 `消息里缺设备标识` | 异常消息里没拼 `host` / `port` |
| C5 `用了 3.7 秒才返回` | `UdpTransportTarget` 少了 `retries=0` |
| C6 `返回了「成功」：'No Such Object currently exists...'` | 没判 `NoSuchObject`，这是今天最值钱的一条 |

---

## 八、第 5 步：如果你手边有真设备

**公司/实验室的交换机、路由器，甚至你家的光猫**（很多光猫默认开 SNMP）都能试：

```powershell
..\.venv\Scripts\python.exe getprobe.py --host 192.168.1.1 --community public
```

几种真实结果，都算正常：

| 现象 | 含义 |
|---|---|
| 打印出型号和 uptime | 通了，恭喜，这条路以后天天用 |
| `No SNMP response received before timeout` | 设备没开 SNMP、端口被 ACL 挡了、或者 community 不对 |
| `No SNMP response` + 但 ping 得通 | 八成是 community 错。**SNMPv2c 认证失败的表现就是超时**，不是「密码错」——这是 D12 要处理的分类问题 |
| `设备拒绝：...` | 设备明确回了错误码，通常是 OID 不存在或权限不足 |

> 试真机时**只能读，不要 WALK 大表**。有些老设备被 WALK 会 CPU 飙高，在现网是事故。

---

## 九、收尾（5 分钟）

**1. 删掉临时文件**

前面第 0 步如果你存了 `.py` 试验文件，删掉。`d8` 目录最后应该只有四个文件：

```
getprobe.py    ← 你写的
fake_agent.py  ← 我给的
check_d8.py    ← 我给的
README.md      ← 这份
```

**2. 提交**

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35
git add week1/d8
git status
git commit -m "D8: pysnmp 真采集，getCmd + TimeTicks + NoSuchObject"
git pushall
```

`git status` 时要看到的是**四个新文件**，不该出现 `__pycache__` 或 `out/`（`.gitignore` 已经排掉了）。

---

## 十、今天真正学到的东西

抛开 SNMP，今天这三个技能在任何「调外部服务」的场景都用得上：

| 技能 | 通用说法 |
|---|---|
| 检查 `error_indication` / `error_status` / 空结果三道门 | **一个响应有多个失败维度，每个都要判** |
| `retries=0` | **知道库的默认值，别被默认值坑** |
| `NoSuchObject` 是类型不是内容 | **「看起来像正常值」的错误最危险，要按类型判而不是按内容猜** |
| 异常消息带 host/port | **错误信息是给人看的，不是给程序看的** |
| 不在下层吞异常 | **异常要在能决策的那一层处理** |

第 5 条后面会反复出现：做 Agent 时，工具函数把错误吞掉、返回一个 `"error"` 字符串给模型，模型就会一本正经地在此基础上编下去。**让错误响亮地失败，是 Agent 可靠性的起点。**

---

## 十一、和 snmpcheck 的关系（先知道，别动手）

你现在 `snmpcheck/probe.py` 里的 `make_simulated_probe` 还是模拟的。D8 做的东西还没接进去——**这是故意的**。

进度安排：

```
D8  独立练 getCmd（今天）        ← 一次只解决一个问题
D9  结果转 dataclass + safe_int
D10 WALK 接口表
D11 落盘 CSV/JSON
D12 错误分类（才有资格做重试决策）
     ↓
     这时才把 probe.py 的模拟通道换成真采集
```

**为什么不在今天直接接进 snmpcheck**：真采集会同时引入「协议问题」和「集成问题」，混在一起时你分不清是 OID 写错了还是参数传错了。分两步走，每步的报错都是单义的。

忍不住想看的话，跑一下这个，它现在读的还是模拟数据：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\snmpcheck
..\.venv\Scripts\python.exe snmpcheck.py status
```

`probe.py` 的注释里写着「D8 会把 `make_simulated_probe` 整个换成真的 pysnmp getCmd」，你今天还不动它——等 D12 把错误分类做完，才有资格决定「哪次失败该重试、哪次该直接放弃」，那时替换才是安全的。
