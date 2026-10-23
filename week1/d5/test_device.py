from models import Device, load_devices, SnmpError
import json
import pytest
from pathlib import Path


def test_sanity() -> None:
    d = Device(name="x", ip="1.1.1.1")
    assert d.name == "x"

def test_load_real_json() -> None:
    devices = load_devices(Path("devices.json"))
    assert len(devices) == 3
    assert devices[0].name == "core-sw-01"

def test_load_empty_list(tmp_path) -> None:
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"count": 0, "devices": []}), encoding="utf-8")
    assert load_devices(p) == []

def test_cpu_boundary() -> None:
    assert Device(name="a", ip="1.1.1.1", cpu=30).is_healthy() is True
    assert Device(name="a", ip="1.1.1.1", cpu=80).is_healthy() is False
    assert Device(name="a", ip="1.1.1.1", cpu=92).is_healthy() is False

def test_invalid_status_raises() -> None:
    with pytest.raises(SnmpError):
        Device.from_dict({"name": "x", "ip": "1.1.1.1", "status": "up1c"})

def test_missing_file_raises_snmp_error(tmp_path) -> None:
    with pytest.raises(SnmpError):
        load_devices(tmp_path / "nope.json")

def test_roundtrip_keeps_all_fields() -> None:
    src = Device(name="agg-sw-03", ip="10.0.0.3", status="up", cpu=70, tags=["core", "switch"])
    back = Device.from_dict(src.to_dict())
    assert back.name == src.name
    assert back.ip == src.ip
    assert back.cpu == src.cpu
    assert back.status == src.status
    # 剩下三个字段也要逐个断言 —— 这是重点