# D2 · 换掉 print：让日志系统真正存在

> 昨天的 D1 已经全绿（6/6）。今天在它的基础上加一层日志，代码不动数据部分。
> **约 20 行，一个函数 + 两处替换。**

---

## 一、为什么 D2 不是"把 print 改成 log"这么简单

你昨天在 `d1/loader.py` 里写了一句：

```python
if data["count"] != len(data["devices"]):
    log.warning("设备数量不对")
```

这段代码**平时没报错，但只要 `count` 对不上就会崩**：

```
NameError: name 'log' is not defined
```

因为 `log` 从来没被定义过——日志系统客观上还不存在。这就是今天要补的东西。

**教训（面试也能讲）**：`print` 挂了只是"少一行输出"，日志挂了往往变成"线上静默失败"。所以日志器必须集中建一次、被所有模块复用。

---

## 二、今天要做两件事

### 任务 1（核心）：实现 `setup_logger()`，约 15 行

打开 `loader.py`，找到 `def setup_logger(...)`，把最后那行 `raise NotImplementedError(...)` 删掉，按 docstring 里的 **7 步**写。docstring 里每一步都写了"为什么"，写完要能讲出来。

### 任务 2：把 2 处 `print` 换成日志

在 `main()` 里搜 `TODO(你·第`，一共两处：

| 位置 | 现在 | 改成 |
|---|---|---|
| `except SnmpError` 分支 | `print(f"[ERROR] {exc}")` | `log.error("读取设备清单失败：%s", exc)` |
| 设备不存在 | `print(f"[ERROR] 设备不存在：{args.device}")` | `log.error("设备不存在：%s", args.device)` |

> `print_table()` 里的 print **不要动**。见下面第四节的边界判断。

---

## 三、验收

```bash
cd week1/d2
python loader.py            # 先看能不能跑通
python check_d2.py          # 自动验收，7 项
```

目标：`通过 7/7`（C1–C6 必做，C7 加练）。

跑通后，日志文件应该有这个效果：

```
11:01:55 | INFO     | snmpcheck | 从 devices.json 加载 3 台设备
11:01:55 | INFO     | snmpcheck | 巡检结束，共输出 3 台
```

而终端上**只有表格**、没有日志行 —— 因为日志走的是 stderr，表格走 stdout。这就是"报告与日志分离"，`python loader.py > report.txt` 时你才会感谢自己。

---

## 四、判断边界：什么该 print，什么该 log

| 内容 | 用哪个 | 理由 |
|---|---|---|
| 表格、json、csv 的内容 | `print` | 是**程序产物**，要能进管道、重定向到文件 |
| 加载了几台、耗时多久 | `log.info` | 过程信息，平时要看、上线可关 |
| 数量不一致、重试 | `log.warning` | 没坏但不对劲 |
| 文件不存在、设备不可达 | `log.error` | 这次操作失败了 |

一句话：**print 是给下游程序的，log 是给运维的人看的。**

---

## 五、五个坑（都在这道题里）

| 现象 | 原因 |
|---|---|
| 同一行日志打印两遍 | 函数被调用两次，`logger.handlers` 没做守卫 —— 第 2 步 |
| `log.info` 什么都不输出，也不报错 | 没 `setLevel`，默认 WARNING 把 INFO 过滤了 —— 第 3 步 |
| 第一次跑就 `FileNotFoundError: out/` | 没 `logfile.parent.mkdir(parents=True, exist_ok=True)` |
| 日志文件里中文是乱码 | `FileHandler` 没写 `encoding="utf-8"`（Windows 默认 gbk） |
| 日志和表格混在一起，重定向后文件里全是日志 | 控制台 handler 用了 `sys.stdout`，应该用 `sys.stderr` |

---

## 六、每天的检查命令（在当天目录里跑）

```bash
cd week1/d2
python -m ruff check .      # 风格
python -m mypy .            # 类型
python check_d2.py          # 验收
```

> 注意 `mypy .` 要在 `d2/` 里面跑，不要在 `week1/` 根目录跑：`d1/loader.py` 和
> `d2/loader.py` 同名，mypy 在根目录会把它们当成同一个模块报 duplicate。

---

## 七、做完后提交

```bash
cd F:/workbuddy_workspace/2026-10-19-10-49-35
git add -A
git commit -m "D2: print 换成 logging，分 INFO/WARNING/ERROR 并落盘 out/run.log"
git pushall
```
