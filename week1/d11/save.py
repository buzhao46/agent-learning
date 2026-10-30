"""D11 · 落盘：同一份采集结果，写成 CSV 和 JSON 两份。

    python save.py --port 1161                 # 假设备
    python save.py --port 1161 --out out       # 指定输出目录
    python save.py --port 1163 --max-rows 5    # 50 口设备只看前 5 行

D9 把结果装进了 dataclass，D10 把整张表读了出来 —— 但它们都只在屏幕上一闪而过。
今天要把它们变成两个能交出去的文件：

    CSV   给人和 Excel（必须 utf-8-sig，否则中文乱码）
    JSON  给程序（D12 的错误分类、将来的比对和入库）

三个 TODO，约 30 行：
    1. to_rows     list[Interface] → CSV 的行（dict）
    2. write_csv   写一份 Excel 能直接双击打开的 CSV
    3. write_json  原子地写一份 JSON（先 tmp，再 os.replace）

已经写好的（不用改）：
    CSV_FIELDS      CSV 的列和列序（唯一定义处）
    slug()          设备名 → 文件名安全的片段
    build_payload() JSON 的整体结构（含 schema 版本和 stats）
    main()          采集 → 落盘 → 打印路径

今天的新知识不在 API，在三个参数和一个动作：
    encoding="utf-8-sig"   CSV 要 BOM，Excel 才认这是 UTF-8
    newline=""             否则 Windows 上每行之间多一个空行
    ensure_ascii=False     否则中文变成 \\u4e0a\\u8054\\u53e3
    tmp + os.replace       产物永远是完整的，不存在「半个 JSON」
"""




from __future__ import annotations
from typing import Literal

import argparse
import csv
import dataclasses
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from health import probe_health
from ifwalk import (
    OID_IF_DESCR,
    OID_IF_IN_OCTETS,
    OID_IF_OPER,
    Interface,
    build_interfaces,
    walk,
)
from snmpio import ProbeError

# CSV 的列和列序。**只在这里定义一次**，DictWriter 和 to_rows 都用它，
# 不要两边各写一份表头 —— 改一个忘一个就是错列事故。
CSV_FIELDS: tuple[Literal['host'], Literal['collected_at'], Literal['index'], Literal['name'], Literal['status'], Literal['in_octets']] = ("host", "collected_at", "index", "name", "status", "in_octets")

# JSON 的结构版本号。三个月后打开一份旧文件，不知道当时的字段是什么意思，
# 这份文件就废了。一个整数，成本为零；将来加字段就变成 2。
SCHEMA_VERSION = 1


# ---------------------------------------------------------------- 1. 摊平
def to_rows(
    host: str,
    rows: list[Interface],
    collected_at: str,
) -> list[dict[str, object]]:
    """把 list[Interface] 摊平成 CSV 的行，每行一个 dict。

    三件事要想清楚：

    1. **in_octets 为 None 时原样写 None。** 不是 "-"，也不是 0。
       fmt_table（D10）里把 None 打成 "-" 是**显示层**的约定，那里是给人看的；
       这里是**存储层**，写 "-" 之后程序读回来只能拿到字符串 "-"，
       「一个包都没收」和「没采到」就永远分不清了。
       csv 模块遇到 None 会写空单元格 —— 正是我们要的。

    2. **每行都要带 host 和 collected_at。** 单独看一行 CSV 时不知道它是
       哪台设备、什么时候采的，这行数据就废了；将来把十台设备的 CSV 拼起来
       分析时，这两列是唯一的主线索。

    3. **键的顺序必须和 CSV_FIELDS 一致**（Python 3.7+ 的 dict 保序，
       DictWriter 按 fieldnames 取，顺序不一致虽然不会报错，但人读起来会错列）。
    """
    return [
        {
            "host": host,
            "collected_at": collected_at,
            "index": row.index,
            "name": row.name,
            "status": row.status,
            "in_octets": row.in_octets,
        }
        for row in rows
    ]


# ---------------------------------------------------------------- 2. CSV
def write_csv(
    path: Path,
    rows: list[dict[str, object]],
    fields: tuple[str, ...] = CSV_FIELDS,
) -> Path:
    """写一份 Excel 能直接打开的 CSV，返回写进去的路径。

    三个都不能少：

        path.parent.mkdir(parents=True, exist_ok=True)
            out/ 在 .gitignore 里，clone 下来的人没有这个目录。
            exist_ok=True 是因为第二次跑时它已经在了。

        encoding="utf-8-sig"
            utf-8-sig = UTF-8 + BOM（EF BB BF）。没有 BOM，Excel 在 Windows 上
            按本地 ANSI（GBK）解码，中文接口名全是乱码。
            这是今天最关键的一个参数。

        newline=""
            csv 模块自己写 \\r\\n 作为行结束；Windows 的文本模式又会把 \\n 翻译成
            \\r\\n。两层叠加就是 \\r\\r\\n —— 每行之间夹一个空行。
            newline="" 关掉翻译，把换行完全交给 csv 模块。

    用 csv.DictWriter(handle, fieldnames=list(fields))，
    先 writeheader() 再 writerows(rows)。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        writer.writerows(rows)
    return path


# ---------------------------------------------------------------- 3. JSON
def write_json(path: Path, payload: dict[str, object]) -> Path:
    """原子地写一份 JSON，返回写进去的路径。

    为什么要绕一道（先写 tmp 再 replace）：

        json.dump 是流式写的。写到 60% 时进程被 Ctrl+C、磁盘满、网络存储断开，
        目标文件就是半截 JSON —— json.load 直接崩，而上一份好的也没了。
        直接覆盖写在「脚本每天定时跑」的场景下迟早出一次这种事故。

        os.replace(tmp, path) 在同一分区内是 rename 系统调用，是原子的：
        要么替换成功拿到完整的新文件，要么没替换还是旧的那个完整文件。
        代价两行，收益是「产物永远是能读的」。

    三个细节：

        encoding="utf-8"（**不要** utf-8-sig）
            带 BOM 的 JSON，json.load 会抛 Unexpected UTF-8 BOM。
            BOM 是给 Excel 的，不是给 JSON 的。

        ensure_ascii=False
            默认是 True，中文会变成 \\u4e0a\\u8054\\u53e3。那是合法 JSON，
            读回来也对，所以你不会收到任何报错 —— 只是人没法看、diff 没法做。

        indent=2 + 末尾一个 \\n
            缩进是为了 git diff 能看；末尾换行是 POSIX 的规矩
            （否则 git 会提示 \\ No newline at end of file）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        json.dump(payload,handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    return path



# ---------------------------------------------------------------- 已写好
def slug(text: str) -> str:
    """设备名 → 文件名安全的片段。

    真机的 sysName 里有空格、冒号、斜杠（"Core SW-01 / 3F"），
    直接拼进文件名不是报错就是写到别的目录去。
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", text).strip("_") or "unknown"


def build_payload(
    host: str,
    health: object,
    interfaces: list[Interface],
    *,
    collected_at: str,
    collected_at_ts: float,
) -> dict[str, object]:
    """组装 JSON 的整体结构。设备信息和接口表放在一份文件里。

    device 用 health.to_dict()（D9 就写好了它，今天终于用上）；
    interfaces 用 dataclasses.asdict 摊平，in_octets=None 会落成 null。

    stats 是**故意冗余**的：它完全能从 interfaces 算出来，但读的人
    （包括明天的你）不用写循环就能看到「4 个口，1 个 down」。
    """
    device = health.to_dict() if hasattr(health, "to_dict") else {}
    return {
        "schema": SCHEMA_VERSION,
        "host": host,
        "collected_at": collected_at,
        "collected_at_ts": collected_at_ts,
        "device": device,
        "interfaces": [dataclasses.asdict(row) for row in interfaces],
        "stats": {
            "total": len(interfaces),
            "up": sum(1 for row in interfaces if row.status == "up"),
            "down": sum(1 for row in interfaces if row.status == "down"),
            "unknown": sum(1 for row in interfaces if row.status == "未知"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="采一台设备的接口表，落盘 CSV + JSON")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=161, help="假设备用 1161")
    parser.add_argument("--community", default="public")
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--max-rows", type=int, default=200, help="每列最多读多少行")
    parser.add_argument("--out", default="out", help="输出目录")
    args = parser.parse_args()

    # 设备级失败（不可达 / community 错）不产生文件 —— 和 D9/D10 一个规矩
    try:
        health = probe_health(
            args.host,
            community=args.community,
            port=args.port,
            timeout=args.timeout,
        )
        descr = walk(
            args.host,
            OID_IF_DESCR,
            community=args.community,
            port=args.port,
            timeout=args.timeout,
            max_rows=args.max_rows,
        )
        status = walk(
            args.host,
            OID_IF_OPER,
            community=args.community,
            port=args.port,
            timeout=args.timeout,
            max_rows=args.max_rows,
        )
        octets = walk(
            args.host,
            OID_IF_IN_OCTETS,
            community=args.community,
            port=args.port,
            timeout=args.timeout,
            max_rows=args.max_rows,
        )
    except ProbeError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1

    interfaces = build_interfaces(descr, status, octets)

    now = datetime.now()
    collected_at = now.isoformat(timespec="seconds")
    collected_at_ts = time.time()

    outdir = Path(args.out)
    base = f"interfaces_{slug(health.sys_name)}_{now.strftime('%Y%m%d-%H%M%S')}"
    csv_path = outdir / f"{base}.csv"
    json_path = outdir / f"{base}.json"

    rows = to_rows(args.host, interfaces, collected_at)
    write_csv(csv_path, rows)
    payload = build_payload(
        args.host,
        health,
        interfaces,
        collected_at=collected_at,
        collected_at_ts=collected_at_ts,
    )
    write_json(json_path, payload)

    print(f"已写入 {csv_path}  ({len(rows)} 行)")
    print(f"已写入 {json_path}  ({len(interfaces)} 个接口, {payload['stats']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
