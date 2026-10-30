# D11 · 落盘：同一份结果，写 CSV 和 JSON

**今天一句话**：昨天把表读出来了，但它只在屏幕上一闪而过。今天要把它变成**两个能交出去的文件**。

同一份采集结果，两种投影：CSV 给人和 Excel，JSON 给程序（明天的对比、告警、入库）。

预计 45–55 分钟。三个 TODO + 一个 `main()`，约 50 行。

---

## 一、先想清楚：为什么是两种格式，不是挑一种

| | CSV | JSON |
|---|---|---|
| 读者 | 运维、Excel、邮件附件 | 下一个程序（D12 的错误分类、将来的入库/比对） |
| 形状 | 扁平，**一行一个接口** | 嵌套：设备信息 + 接口列表 + 统计 |
| 缺失值 | 空单元格 | `null` |
| 能不能表达层级 | 不能 | 能 |

所以「写两种」不是重复劳动，是**同一个事实的两种投影**。真出事的时候，你要能一边把 CSV 甩到群里，一边让脚本读 JSON 做比对。

落盘这个动作，有三条硬要求，今天的三个 TODO 就是围绕它们设计的：

1. **不能写坏**：写到一半崩了，不能把上一份好的也弄丢
2. **不能覆盖历史**：巡检是时间序列，第二次跑把第一次冲掉，就没法比对了
3. **中文不能乱码**：Excel 打开是「上联口-核心」，不是 `ä¸Šè”`

---

## 二、今天的目录

```
week1/d11/
├── save.py         ← 你写，3 个 TODO（main() 已经写好了）
├── check_d11.py    ← 7 项验收
├── ifwalk.py       ← D10 的成品，原样复制
├── health.py       ← D9 的成品，原样复制（今天终于用上它了）
├── snmpio.py       ← D9 的，原样复制
├── fake_agent.py   ← D10 的，原样复制
└── README.md       ← 这份
```

先把四个文件复制过来（PowerShell）：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1
mkdir d11
copy d10\ifwalk.py   d11\
copy d10\health.py   d11\
copy d10\snmpio.py   d11\
copy d10\fake_agent.py d11\
```

**不要改**这四个复制过来的文件，也不要改 `check_d11.py`。它们是对照基准——`ifwalk.py` 里的 `walk` / `build_interfaces` / `fmt_table`、`health.py` 里的 `probe_health` / `to_dict`，今天全部直接调用。

你只写 `save.py` 一个新文件。

---

## 三、第 0 步：亲眼看见三个坑（10 分钟，别跳过）

今天一半的坑在这一步，而且它们**全都不会报错**——文件写出来了，只是打开是坏的。

### 0.1 起一台假交换机

开一个 PowerShell 窗口，**这个窗口别关**：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d11
..\.venv\Scripts\python.exe fake_agent.py --port 1161
```

看到这行就对了：

```
假设备已启动：127.0.0.1:1161  name=core-sw-01  community=public
```

### 0.2 第一个坑：Excel 打开 utf-8 的 CSV 会乱码

```powershell
..\.venv\Scripts\python.exe -c "import pathlib; p=pathlib.Path('out/_t1.csv'); p.parent.mkdir(exist_ok=True); p.write_text('index,name\n3,上联口-核心\n', encoding='utf-8'); print(p.read_bytes()[:20])"
```

```
b'index,name\n3,\xe4\xb8\x8a\xe8\x81\x94\xe5\x8f\xa3'
```

字节完全正确，是标准 UTF-8。但**双击用 Excel 打开，中文是乱码**——因为 Excel 在 Windows 上没有 BOM 时按本地 ANSI（GBK）解码，它不知道这是 UTF-8。

BOM 那三个字节长这样，加上它 Excel 才认：

```powershell
..\.venv\Scripts\python.exe -c "import pathlib; p=pathlib.Path('out/_t2.csv'); p.write_text('index,name\n3,上联口-核心\n', encoding='utf-8-sig'); print(p.read_bytes()[:20])"
```

```
b'\xef\xbb\xbfindex,name\n3,\xe4\xb8\x8a...'
   ↑ 这三个字节就是 BOM
```

**`utf-8-sig` = UTF-8 + BOM。** 这是今天最关键的一个参数，写 CSV 就用它。

### 0.3 第二个坑：不传 `newline=""`，每行之间多一个空行

```powershell
..\.venv\Scripts\python.exe -c "import csv,pathlib; p=pathlib.Path('out/_t3.csv'); f=p.open('w',encoding='utf-8-sig'); csv.writer(f).writerows([['index','name'],[3,'上联口-核心']]); f.close(); print(p.read_bytes())"
```

你会看到 `...\r\r\n` —— 两个回车。原因：`csv` 模块自己写 `\r\n` 作为行结束，而 Windows 的文本模式又把 `\n` 翻译成 `\r\n`，于是 `\r` + `\r\n`。

在 Excel、pandas 里表现为**每行之间夹一个空行**。解决办法只有一条：`open(..., newline="")`，把换行翻译关掉，交给 csv 模块自己管。

### 0.4 第三个坑：JSON 默认把中文转成 `\uXXXX`

```powershell
..\.venv\Scripts\python.exe -c "import json; print(json.dumps({'name':'上联口-核心'})); print(json.dumps({'name':'上联口-核心'}, ensure_ascii=False))"
```

```
{"name": "\u4e0a\u8054\u53e3-\u6838\u5fc3"}
{"name": "上联口-核心"}
```

第一行**是合法 JSON**，读回来也对，所以你不会收到任何报错。但人没法看、diff 没法做、日志里全是转义串。`ensure_ascii=False` 是 JSON 落盘的标配。

### 0.5 顺手记住一个对比

| 文件 | 编码 | 为什么 |
|---|---|---|
| CSV | `utf-8-sig` | 给 Excel 看，要 BOM |
| JSON | `utf-8`（**不要** BOM） | 给 `json.load` 读，有 BOM 会崩 |

```
json.decoder.JSONDecodeError: Unexpected UTF-8 BOM (decode using utf-8-sig)
```

两边都用 `utf-8-sig` 是很常见的顺手一写，代价是 JSON 从此读不进来。

---

## 四、第 1 步：`to_rows`（dataclass → CSV 行，10 分钟）

```python
CSV_FIELDS = ("host", "collected_at", "index", "name", "status", "in_octets")


def to_rows(host: str, rows: list[Interface], collected_at: str) -> list[dict[str, object]]:
    """把 list[Interface] 摊平成 CSV 的行。"""
    return [
        {
            "host": host,
            "collected_at": collected_at,
            "index": row.index,
            "name": row.name,
            "status": row.status,
            "in_octets": row.in_octets,   # None 就让它空着
        }
        for row in rows
    ]
```

三个要点：

1. **保留 `None`，不要写成 `"-"`。** `fmt_table`（D10）里把 `None` 打成 `-` 是**显示层**的约定，那里是给人看的；落盘是**存储层**，写 `-` 之后程序读回来只能拿到字符串 `"-"`，「一个包都没收」和「没采到」就再也分不清了。`csv` 模块遇到 `None` 会写空单元格，正是我们要的。同理不要写 `0`。
2. **每行都带 `host` 和 `collected_at`。** 单独看一行 CSV 时，不知道它是哪台设备、什么时候采的——这行数据就废了。将来把十台设备的 CSV 拼一起分析时，这两列是唯一的主线索。
3. **字段顺序由 `CSV_FIELDS` 一处定义。** `DictWriter` 靠它定列序，别在 writer 和 rows 里各写一遍表头，改一个忘一个是最常见的错列事故。

---

## 五、第 2 步：`write_csv`（10 分钟）

```python
def write_csv(path: Path, rows: list[dict[str, object]], fields: tuple[str, ...]) -> Path:
    """写一份 Excel 能直接打开的 CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)          # ①
    with path.open("w", encoding="utf-8-sig", newline="") as handle:   # ②③
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        writer.writerows(rows)
    return path
```

- ① **`mkdir(parents=True, exist_ok=True)`**。`out/` 在 `.gitignore` 里，clone 下来的人没有这个目录，不建就 `FileNotFoundError`。`exist_ok=True` 是因为第二次跑时它已经在了。
- ② **`encoding="utf-8-sig"`**（0.2 那个坑）。
- ③ **`newline=""`**（0.3 那个坑）。

`csv` 模块只用这三行。今天的知识不在 API，在这三个参数。

### 自检

```powershell
..\.venv\Scripts\python.exe -c "import ifwalk as w, save; print(save.to_rows('core-sw-01', [w.Interface(1,'GE0/0/1','up',100), w.Interface(2,'上联口-核心','未知',None)], '2026-10-28T10:49:35'))"
```

先确认能 import 通，并且第二行的 `in_octets` 是 `None`（不是 `-`、不是 `0`）。写完第一步就能跑这条。

---

## 六、第 3 步：`write_json`（15 分钟）

```python
def write_json(path: Path, payload: dict[str, object]) -> Path:
    """原子地写一份 JSON：要么完整的新文件，要么还是旧文件，不会是半个。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")            # ①
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)   # ②
        handle.write("\n")
    os.replace(tmp, path)                               # ③
    return path
```

- ① **先写临时文件**。`json.dump` 是流式写的，写到 60% 时进程被 Ctrl+C、网络存储断开、磁盘满，目标文件就是半截 JSON。
- ③ **`os.replace(tmp, path)` 是原子操作**（同一个分区内是 rename 系统调用）。要么替换成功拿到完整新文件，要么没替换还是旧的那个完整文件。代价是两行代码，收益是「产物永远是能读的」。
- ② **`ensure_ascii=False`**（0.4 那个坑）。`indent=2` 是为了 `git diff` 能看、人能读；最后补一个 `\n` 是 POSIX 的规矩（`git` 会提示 `\ No newline at end of file`）。

> 直接 `path.open("w")` 覆盖写，在「采集脚本每天定时跑」这个场景下迟早会出一次事故：定时器杀死进程时正好写了一半，于是当天的数据没了，**上一周的也没了**。巡检脚本最怕这种静默的数据丢失。

### payload 长什么样

```python
payload = {
    "schema": 1,                       # ①
    "host": host,
    "collected_at": collected_at,      # "2026-10-28T10:49:35"，人能读
    "collected_at_ts": time.time(),    # 浮点，程序排序用
    "device": health.to_dict(),        # D9 写的那个，今天终于用上
    "interfaces": [dataclasses.asdict(r) for r in interfaces],   # ②
    "stats": {                         # ③
        "total": len(interfaces),
        "up": sum(1 for r in interfaces if r.status == "up"),
        "down": sum(1 for r in interfaces if r.status == "down"),
        "unknown": sum(1 for r in interfaces if r.status == "未知"),
    },
}
```

- ① **`schema` 版本号**。三个月后你打开一份旧 JSON，不知道当时的字段是什么意思，这份文件就废了。一个整数，成本为零。将来加字段就变成 2。
- ② `dataclasses.asdict()` 直接把 frozen dataclass 摊成 dict，`in_octets=None` 会落成 `null`——正是我们要的保真。
- ③ `stats` 是**顺手加的冗余**。它完全可以由 `interfaces` 算出来，但读的人（包括明天的你）不用写循环就能看到「4 个口，1 个 down」。落盘文件是给未来的自己看的，冗余一点没关系。

> 想连 `ifOperStatus` 的**原始数字**一起存，得回 D10 给 `Interface` 加一个字段。**今天不要动它** —— 那是「schema 演化」的问题，`schema: 1` 就是为那天准备的。

---

## 七、第 4 步：文件名、`main()`（15 分钟）

```python
def slug(text: str) -> str:
    """设备名 → 文件名安全的片段。真机的 sysName 里会有空格、冒号、斜杠。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", text).strip("_") or "unknown"
```

文件名用 `f"interfaces_{slug(name)}_{stamp}.csv"` / `.json`，`stamp = datetime.now().strftime("%Y%m%d-%H%M%S")`。

**为什么必须带时间戳**：巡检是时间序列。文件名写死成 `interfaces.csv`，第二次跑就把第一次覆盖了——而「上次 vs 这次」正是你后面要做的事情。等 D12 之后要做增量对比时，你会发现历史文件比代码值钱。

`main()` 的流程（`argparse` 参数照抄 `ifwalk.py` 的 `--host/--port/--community/--timeout/--max-rows`，再加一个 `--out` 默认 `out`）：

1. `probe_health(host, ...)` → 设备信息。**失败就 `except ProbeError` → 打印 `[失败]` → 返回 1**（和 D9/D10 一致：设备级失败不产生文件）
2. `walk` 三列 → `build_interfaces` → 接口表
3. `write_csv` + `write_json`
4. 打印两个路径和行数，返回 0

```powershell
..\.venv\Scripts\python.exe save.py --port 1161
```

```
已写入 out\interfaces_core-sw-01_20261028-104935.csv  (4 行)
已写入 out\interfaces_core-sw-01_20261028-104935.json (4 个接口, up=3 down=1)
```

---

## 八、跑验收

```powershell
..\.venv\Scripts\python.exe check_d11.py
```

目标 **7/7**。脚本会自己起三台假设备（正常 / 缺状态 / 脏 uptime），跑完自动关掉。
C1–C4 只测「写文件」这件事，不需要设备；C5–C7 要真采一次。

| 项 | 测什么 |
|---|---|
| C1 | `to_rows` 六列齐全、`None` 原样保留（不是 `-` / `0`） |
| C2 | CSV 有 BOM、无 `\r\r\n`、中文正常 |
| C3 | JSON 能 `loads`（说明没 BOM）、中文非转义、`None`→`null`、有 `schema` |
| C4 | 原子写：目录里没有 `.tmp` 残留 |
| C5 | CLI 连跑两次是 4 个文件，没互相覆盖 |
| C6 | 缺状态行 → 「未知」但字节数还在；脏 uptime → `null` |
| C7 | `python save.py --port ...` 端到端　[加练] |

验收脚本只告诉你「过没过」。下面这七条命令是**手工版**——跑一遍你就知道每一条到底在验什么。
建议至少手工走第 1、3、4 条，它们对应的三个坑，只有亲眼看过才记得住。

### 1. CSV 有 BOM（Excel 不乱码的前提）

```powershell
..\.venv\Scripts\python.exe -c "import pathlib; p=sorted(pathlib.Path('out').glob('interfaces_*.csv'))[-1]; print(p.name, open(p,'rb').read(3)==b'\xef\xbb\xbf')"
```

要打印 `True`。

### 2. CSV 没有多余空行

```powershell
..\.venv\Scripts\python.exe -c "import pathlib; p=sorted(pathlib.Path('out').glob('interfaces_*.csv'))[-1]; print('空行:', open(p,'rb').read().count(b'\r\r\n'))"
```

要打印 `空行: 0`。

### 3. JSON 里中文是正常的，不是 `\u4e0a`

```powershell
..\.venv\Scripts\python.exe -c "import json,pathlib; d=json.loads(sorted(pathlib.Path('out').glob('interfaces_*.json'))[-1].read_text(encoding='utf-8')); print(d['device']['sys_name']); print([i['name'] for i in d['interfaces']])"
```

要看到 `core-sw-01` 和 `['GE0/0/1', 'GE0/0/2', '上联口-核心', 'XGE0/0/4']`。

> 这一步能跑通，本身就说明 JSON **没有** BOM——有 BOM 的话 `json.loads` 会直接抛 `Unexpected UTF-8 BOM`。

### 4. 缺失值一路保真到了磁盘

起一台缺 status 行的设备：

```powershell
..\.venv\Scripts\python.exe fake_agent.py --port 1162 --miss-status
..\.venv\Scripts\python.exe save.py --port 1162
..\.venv\Scripts\python.exe -c "import json,pathlib; d=json.loads(sorted(pathlib.Path('out').glob('interfaces_*.json'))[-1].read_text(encoding='utf-8')); print([(i['index'], i['status'], i['in_octets']) for i in d['interfaces']])"
```

（这几条自检都取 `out/` 里**最新**的一份文件，所以每跑一次 `save.py` 就立刻执行下一条验证。）

要看到第 3 个接口是 `('未知', 4294967100)` —— **状态未知，但字节数还在**（D10 的按行号合并保住了它）。

再起一台脏 uptime 的（D9 那台）：

```powershell
..\.venv\Scripts\python.exe fake_agent.py --port 1163 --uptime-bad
..\.venv\Scripts\python.exe save.py --port 1163
..\.venv\Scripts\python.exe -c "import json,pathlib; d=json.loads(sorted(pathlib.Path('out').glob('interfaces_*.json'))[-1].read_text(encoding='utf-8')); print(d['device']['uptime_ticks'], repr(d['device']['uptime_text']))"
```

要打印 `None '未知'`。**不是** `-1`、不是 `2147483647`、不是 `0`——D9 的 `safe_int` 挡住的脏值，落到磁盘上仍然是 `null`。这是一条链：采集 → 解析 → 落盘，任何一环把 `None` 变成 `0`，后面就再也修不回来。

### 5. 没有 `.tmp` 残留

```powershell
dir out\*.tmp
```

要「找不到文件」。有残留说明 `os.replace` 那步没走到（异常了），去查 `write_json`。

### 6. 连跑两次，是 4 个文件不是 2 个

```powershell
..\.venv\Scripts\python.exe save.py --port 1161
..\.venv\Scripts\python.exe save.py --port 1161
dir out\
```

如果有覆盖，说明文件名写死了，或者时间戳精度不够（只到 `%H` 就是一小时一冲）。

注意时间戳精确到**秒**，两条命令之间要隔一秒以上才看得出区别——`check_d11.py` 的 C5 中间就 `sleep` 了 1.2 秒。真要一秒内跑两次也不冲突，得把精度提到毫秒，但巡检是分钟级的，秒够了。

### 7. 人工最后一眼

双击 `out\interfaces_*.csv`，Excel 里「上联口-核心」应该是正常中文。前六条命令全绿也替代不了这一眼——**乱码只有人能看出来**。

---

## 九、失败对照表

| 现象 | 原因 | 解法 |
|---|---|---|
| Excel 打开中文是 `ä¸Šè”å£` 之类 | 用了 `utf-8` 无 BOM | CSV 改 `utf-8-sig` |
| CSV 每行之间夹一个空行 | `open()` 没传 `newline=""` | 补上 |
| `json.load` 报 `Unexpected UTF-8 BOM` | JSON 也用了 `utf-8-sig` | JSON 用 `utf-8` |
| JSON 里中文是 `\u4e0a\u8054` | `ensure_ascii` 默认 True | `ensure_ascii=False` |
| `FileNotFoundError: out\xxx.csv` | 目录不存在 | `mkdir(parents=True, exist_ok=True)` |
| 第二次跑只剩一份文件 | 文件名写死 / 时间戳精度不够 | 文件名带 `%Y%m%d-%H%M%S` |
| `in_octets` 缺的那行是 `0` 或 `"-"` | 落盘前把 `None` 转成了占位值 | CSV 留空（`None` 原样传），JSON 留 `null` |
| JSON 只写了一半，程序读就崩 | 直接覆盖写被中断 | tmp + `os.replace` |
| CSV 列错位（名字配上了别人的状态） | `DictWriter` 的 `fieldnames` 和 dict 的键对不上 | 表头只用 `CSV_FIELDS` 这一处常量 |
| `TypeError: Object of type set is not JSON serializable` | payload 里混进了非 JSON 类型（set / datetime / bytes） | 转成 list / ISO 字符串 / str |

---

## 十、三个设备，看三种落盘结果

```powershell
# 1. 正常设备：4 个口，第 3 个 down
..\.venv\Scripts\python.exe fake_agent.py --port 1161
..\.venv\Scripts\python.exe save.py --port 1161

# 2. 少回一行 status：第 3 个「未知」，但 in_octets 还在
..\.venv\Scripts\python.exe fake_agent.py --port 1162 --miss-status
..\.venv\Scripts\python.exe save.py --port 1162

# 3. 50 个接口：验证落盘不是只处理 4 行
..\.venv\Scripts\python.exe fake_agent.py --port 1164 --many
..\.venv\Scripts\python.exe save.py --port 1164
```

打开第 2 份 JSON，看 `stats`：应该是 `{'total': 4, 'up': 3, 'down': 0, 'unknown': 1}`。
打开第 3 份 CSV，最后一行应该是第 50 个接口——顺便体会一下为什么真实巡检一定要有 `--max-rows`。

---

## 十一、收尾

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35
git add week1/d11
git status
git commit -m "D11: 落盘 CSV(utf-8-sig)+JSON(原子写)，文件名带时间戳"
git pushall
```

`git status` 里应该有：`save.py`、`check_d11.py`、`ifwalk.py`、`health.py`、`snmpio.py`、`fake_agent.py`、`README.md`。
**没有** `out/`、没有 `_t1.csv` 那些试手文件（有的话删掉）、没有 `__pycache__`。

> `out/` 在 `.gitignore` 里是故意的：产物不进仓库，进仓库的是**能重新产出这些文件的代码**。

---

## 十二、今天学到的四件事（面试能讲的那种）

1. **落盘是「投影」，不是「保存」**。同一份事实按读者分成两种形状：CSV 扁平给人和 Excel，JSON 嵌套给程序。选一种都会让另一半人难受。
2. **BOM 是给 Excel 的，不是给 JSON 的**。`utf-8-sig` 和 `utf-8` 差三个字节，一边是乱码，一边是 `JSONDecodeError`。这类「参数对了才对、错了也不报错」的东西，只能靠记住。
3. **原子写：tmp + `os.replace`**。产物文件永远是「完整的」，要么新的要么旧的，不存在中间态。定时任务的产物尤其需要这条。
4. **缺失值的保真是一条链**。设备没回 → `safe_int` 给 `None` → `build_interfaces` 给「未知」/ `None` → CSV 空单元格 / JSON `null`。任何一环把它写成 `0` 或 `-`，后面所有对比和告警都会建立在谎言上。

顺带一条工程习惯：**文件名带时间戳 + 内容带 `schema` 版本**。前者保住历史，后者保住将来读得懂。

---

## 十三、D12 预告

现在所有失败都归到 `ProbeError` 一个桶里：超时、community 错、OID 不存在、半张表——**长得一模一样**。后果是重试策略没法写：超时该重试，community 错重试一万次也是错。

D12 就是把这一个桶拆开：可重试 / 不可重试 / 部分成功。今天落盘的 JSON 会多一个 `errors` 字段——那时你会发现，把错误也写进产物，比只写「成功的数据」有用得多。
