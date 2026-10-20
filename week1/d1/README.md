# D1 · 数据外置（SEED → devices.json）

> 对应 `python_21days_drill.html` 的 D1，约 30 行代码。
> 验收标准一句话：**删掉 SEED 后仍能跑出同样结果。**

## 你要做的事

打开 `loader.py`，只补一个函数：`load_devices()`（8~12 行，docstring 里给了 4 步）。

其余全部写好了，不用改：`Device` / `from_dict` / `save_devices` / `print_table` / `main`。

## 三条命令

```bash
cd week1/d1

python loader.py         # 现在会打印 [TODO] 提示 —— 那是留给你动手的位置
python check_d1.py       # 自动验收，跑它就知道过没过
python loader.py --file nope.json   # 看错误处理分支
```

现在跑 `check_d1.py` 是 **1/6**（只有「devices.json 合法」这一条过），补完 `load_devices()` 应该是 **5/6**，再补上 `from_dict` 里那个 2 行加练就是 **6/6**。

## 四个坑（先知道，能省半小时）

| 坑 | 表现 | 解法 |
|---|---|---|
| 忘了 `encoding="utf-8"` | Windows 上中文乱码 | `path.read_text(encoding="utf-8")` |
| 用相对路径 `"devices.json"` | 换个目录跑就找不到文件 | 已用 `Path(__file__).with_name(...)`，注意理解这行为什么稳 |
| 直接 `json.load(open(...))` | 文件句柄不关、异常时泄漏 | 用 `read_text()`，或 `with open(...) as f:` |
| 把 `FileNotFoundError` 直接抛给用户 | 一屏堆栈，看不懂 | `raise SnmpError(...) from exc`，保留因果链 |

## 做完怎么算过

```bash
python check_d1.py
# 通过 5/6 或 6/6，结论里写着「D1 完成」
python loader.py
# 输出必须和 day1_types.py 一模一样（同样的表、同样的「异常 2 台」）
```

两者并排跑一遍对比，一致就真过了。

## 明天（D2）预告

把 `print_table` 里的 `print` 换成 `logging`，分成 INFO / WARNING / ERROR 三级，同时写进 `out/run.log`。
提示：`week1/snippets.py` 里已经有 `setup_logger()`，直接 import，不用重写。
