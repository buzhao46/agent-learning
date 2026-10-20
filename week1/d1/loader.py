"""D1 · 数据外置：把 SEED 从代码里挪到 devices.json

现状：day1_types.py 里 SEED 是硬编码的 3 台设备。
目标：改成启动时从 devices.json 读，代码里不再出现设备数据。

本文件已写好 70%，你只需要补 load_devices() 一个函数（8~12 行）。

跑法：
    cd week1/d1
    python loader.py                    # 读 devices.json 打印表格
    python loader.py --device core-sw-01
    python loader.py --file nope.json   # 看错误处理
    python check_d1.py                  # 自动验收，跑它就知道过没过
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

Status = Literal["up", "down"]
DEFAULT_FILE = Path(__file__).with_name("devices.json")


class SnmpError(RuntimeError):
    """配置错误 / 设备不存在都抛这个，调用方按需捕获。"""


@dataclass
class Device:
    name: str
    ip: str
    status: Status = "up"
    cpu: int = 0
    tags: list[str] = field(default_factory=list)

    def is_healthy(self, cpu_limit: int = 80) -> bool:
        return self.status == "up" and self.cpu < cpu_limit

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Device:
        """json 里的一条 dict -> Device。

        【已写好 · 建议读懂】
        必填字段用 raw["name"] 硬取：缺字段直接 KeyError，比 raw.get("x", "")
        悄悄变成空字符串更容易排查。可选字段才用 .get(key, 默认值)。

        TODO(你 · 加练，2 行)：现在非法 status（比如 "UP"、"downn"）会悄悄溜进下游。
        在下面加一段判断：非法值 raise SnmpError(f"非法状态：{status!r}")

        顺带一个类型注解的细节：raw.get() 返回 Any，所以这里不写注解 mypy 不吭声；
        一旦你写成 status: str = ...，mypy 会提示 str 不能赋给 Literal["up","down"]。
        那个报错是好事——说明注解真的在起作用。想消掉它，就写
            status = cast(Status, raw.get("status", "up"))
        """
        status = cast(Status, raw.get("status", "up"))
        if status not in ("up", "down"):
            raise SnmpError(f"非法状态：{status!r}")
        return cls(
            name=raw["name"],
            ip=raw["ip"],
            status=status,
            cpu=int(raw.get("cpu", 0)),
            tags=list(raw.get("tags", [])),
        )


def load_devices(path: Path = DEFAULT_FILE) -> list[Device]:
    """读 devices.json，返回设备列表。

    【你的任务 · 8~12 行】按这 4 步写：

      1) 文件不存在 -> raise SnmpError(f"配置文件不存在：{path}")
         两种写法选一个，选你能讲清理由的那个：
            a. if not path.exists(): raise ...
            b. try: 读文件 / except FileNotFoundError: raise SnmpError(...) from exc
            （提示：b 更推荐——它没有「先检查再使用」的时间窗，而且 from exc 保留原始堆栈）

      2) 读文本：path.read_text(encoding="utf-8")
         encoding 必须写，Windows 默认是 gbk，中文会乱码。

      3) 解析：json.loads(...)；格式坏了会抛 json.JSONDecodeError
         —— 把它也包成 SnmpError 吗？先让它抛出去，运行一次看看堆栈长什么样，
            再决定要不要包。（这是 D5 写测试时要回答的问题）

      4) 取出 data["devices"]，逐条 Device.from_dict(raw)，收集成 list 返回。
         推荐一行列表推导：[Device.from_dict(raw) for raw in data["devices"]]

    写完把下面那行 raise 删掉。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SnmpError(f"配置文件不存在：{path}") from exc
    data = json.loads(text)
    if data["count"] != len(data["devices"]):
        log.warning("设备数量不对")
    return [Device.from_dict(raw) for raw in data["devices"]]


def save_devices(devices: list[Device], path: Path = DEFAULT_FILE) -> None:
    """反向操作：把内存里的设备写回 json。

    【已写好】用途：生成初始 devices.json，或以后导出采集结果。
    注意 indent=2 + ensure_ascii=False：中文不转义、人看得懂、git diff 干净。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"count": len(devices), "devices": [d.to_dict() for d in devices]}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def print_table(devices: list[Device]) -> None:
    """【已写好】打印表格，格式和 day1_types.py 完全一致，方便你并排对比。"""
    print(f"\n{'设备':<14}{'IP':<14}{'状态':<8}{'CPU':>5}  健康")
    print("-" * 52)
    for d in devices:
        flag = "✓" if d.is_healthy() else "✗"
        print(f"{d.name:<14}{d.ip:<14}{d.status:<8}{d.cpu:>5}  {flag}")
    bad = [d.name for d in devices if not d.is_healthy()]
    print(f"\n共 {len(devices)} 台，异常 {len(bad)} 台：{', '.join(bad) or '无'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="D1 · 从 devices.json 读设备")
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="设备清单 json 路径")
    parser.add_argument("--device", help="只看某台设备，例如 core-sw-01")
    args = parser.parse_args()

    try:
        devices = load_devices(Path(args.file))
    except NotImplementedError as exc:  # 你还没动手时给个友好提示，而不是一屏堆栈
        print(f"[TODO] {exc}")
        print("       补完后重跑：python loader.py")
        return 3
    except SnmpError as exc:  # 只捕获预期内的异常，别写裸 except
        print(f"[ERROR] {exc}")
        return 2

    picked = devices
    if args.device:
        picked = [d for d in devices if d.name == args.device]
        if not picked:
            print(f"[ERROR] 设备不存在：{args.device}")
            return 2

    print_table(picked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
