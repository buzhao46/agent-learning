"""数据层：设备的数据结构与磁盘读写。不掺命令行，也不往屏幕上输出"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from log_setup import get_log

Status = Literal["up", "down"]

HERE = Path(__file__).parent
DEFAULT_FILE = HERE / "devices.json"

class SnmpError(RuntimeError):
    """配置错误 / 设备不存在都抛在这个，调用方按需捕获"""


# 数据模型
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
        status = cast(Status, raw.get("status", "up"))
        if status not in ("up", "down"):
            raise SnmpError(f"非法状态： {status!r}")
        return cls(
            name=raw["name"],
            ip=raw["ip"],
            status=status,
            cpu=int(raw.get("cpu", 0)),
            tags=list(raw.get("tags",[])),
        )

# 读写
def load_devices(path: Path = DEFAULT_FILE) -> list[Device]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SnmpError(f"配置文件不存在：{path}") from exc
    data = json.loads(text)
    if data["count"] != len(data["devices"]):
        get_log().warning("devices.json 里 count=%s，实际 %s 台", data["count"], len(data["devices"]))

    return [Device.from_dict(raw) for raw in data["devices"]]

def save_devices(devices: list[Device], path: Path = DEFAULT_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"count": len(devices), "devices": [d.to_dict() for d in devices]}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
