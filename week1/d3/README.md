# D3 · 加参数：--limit / --verbose

> 代码是 D2 的成品（能跑），今天**只改 `main()`**，别动 `setup_logger` / `load_devices` / `print_table`。
> 验收脚本 `check_d3.py` 有 7 项，跑它就知道哪一步没做到。

---

## 要做成什么样

| 参数 | 行为 |
|---|---|
| `--limit N` | 只输出前 N 台；N 必须是 >= 1 的整数；N 超过总数不算错，全输出 |
| `--verbose` / `-v` | 日志级别降到 DEBUG，多打逐台明细 |
| `--device X` | 已有，保持不动 |

---

## 五步改法（详细说明见对话，这里只做索引）

1. `get_log()` 加 `level` 参数（默认 INFO），传给 `setup_logger(level)`
2. `argparse` 加 `--limit`（`type=int`）和 `-v/--verbose`（`action="store_true"`）
3. `parse_args()` 之后先校验 `--limit`，非法就 `parser.error(...)`（退出码 2）
4. 决定级别：`level = logging.DEBUG if args.verbose else logging.INFO`，再 `get_log(level)`
5. `print_table()` 之前：先 `--limit` 截断（截了就 `log.warning`），再逐台 `log.debug`

---

## 验收

```bash
cd week1/d3
python check_d3.py          # 目标 7/7
python -m ruff check .
python -m mypy .
```

基线（未动手）是 1/7，只有 C6「不带 verbose 不打 DEBUG」会白捡着过。

---

## 三个坑

| 现象 | 原因 |
|---|---|
| `--limit 0` 不报错，直接输出空表 | 判据写成 `if args.limit and args.limit < 1`——`0` 是 falsy，被跳过了。要用 `is not None` |
| 加了 `--verbose` 日志还是只有 INFO | 级别必须在 `get_log()` **之前**决定；`setup_logger` 的 handlers 守卫会让第二次调用不改级别 |
| `--limit -1` 出现 `Traceback` | 用 `parser.error("...")` 报错，别 `raise ValueError` 让异常冒出去 |
