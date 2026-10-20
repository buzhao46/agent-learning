# -*- coding: utf-8 -*-
"""
SNMP 客户端适配层
-----------------
优先使用 pysnmp 发起真实 SNMP GET / WALK；
- 真实设备：直接对目标 IP 的 161 端口发请求
- 模拟设备：返回内置/按名字生成的仿真数据，保证 demo 开箱即用

标准 MIB-II OID：
  sysDescr   1.3.6.1.2.1.1.1.0
  sysUpTime  1.3.6.1.2.1.1.3.0
  sysName    1.3.6.1.2.1.1.5.0
  ifDescr    1.3.6.1.2.1.2.2.1.2
  ifOperStatus 1.3.6.1.2.1.2.2.1.8   (1=up, 2=down)
  ifInErrors 1.3.6.1.2.1.2.2.1.14
  ifOutErrors 1.3.6.1.2.1.2.2.1.20
"""

try:
    from pysnmp.hlapi import (
        SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
        ObjectType, ObjectIdentity, getCmd, nextCmd,
    )
    HAS_PYSNMP = True
except Exception:  # pysnmp 未安装或版本不兼容
    HAS_PYSNMP = False

# 复用同一个 engine，减少开销
_ENGINE = SnmpEngine() if HAS_PYSNMP else None

OID = {
    "sysDescr": "1.3.6.1.2.1.1.1.0",
    "sysUpTime": "1.3.6.1.2.1.1.3.0",
    "sysName": "1.3.6.1.2.1.1.5.0",
    "ifDescr": "1.3.6.1.2.1.2.2.1.2",
    "ifOperStatus": "1.3.6.1.2.1.2.2.1.8",
    "ifInErrors": "1.3.6.1.2.1.2.2.1.14",
    "ifOutErrors": "1.3.6.1.2.1.2.2.1.20",
}


class SnmpError(Exception):
    pass


def _mp_model(version: str) -> int:
    v = (version or "2c").lower().replace("v", "")
    return 0 if v in ("1",) else 1   # 0=SNMPv1, 1=SNMPv2c


def snmp_get(ip: str, oid: str, community: str = "public",
             version: str = "2c", timeout: float = 2.0):
    """真实 SNMP GET，返回值的字符串表示"""
    if not HAS_PYSNMP:
        raise SnmpError("pysnmp 未安装，无法发起真实 SNMP 请求")
    iterator = getCmd(
        _ENGINE,
        CommunityData(community, mpModel=_mp_model(version)),
        UdpTransportTarget((ip, 161), timeout=timeout, retries=1),
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
    )
    error_indication, error_status, _error_index, var_binds = next(iterator)
    if error_indication:
        raise SnmpError(str(error_indication))
    if error_status:
        raise SnmpError(error_status.prettyPrint())
    for _name, val in var_binds:
        return val.prettyPrint()
    raise SnmpError("空响应")


def snmp_walk(ip: str, oid: str, community: str = "public",
              version: str = "2c", timeout: float = 2.0, max_rows: int = 300):
    """真实 SNMP WALK，返回 [(oid, value), ...]"""
    if not HAS_PYSNMP:
        raise SnmpError("pysnmp 未安装，无法发起真实 SNMP 请求")
    results = []
    for (error_indication, error_status, _error_index, var_binds) in nextCmd(
        _ENGINE,
        CommunityData(community, mpModel=_mp_model(version)),
        UdpTransportTarget((ip, 161), timeout=timeout, retries=1),
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
        lexicographicMode=False,
    ):
        if error_indication:
            raise SnmpError(str(error_indication))
        if error_status:
            raise SnmpError(error_status.prettyPrint())
        for name, val in var_binds:
            results.append((str(name), val.prettyPrint()))
            if len(results) >= max_rows:
                return results
    return results


def test_connectivity(ip: str, community: str = "public", version: str = "2c"):
    """连通性/凭据测试：读 sysDescr"""
    value = snmp_get(ip, OID["sysDescr"], community, version, timeout=1.5)
    return {"ok": True, "sysDescr": str(value)[:120]}


# ============================================================
# 模拟数据（开箱即用，便于没有真实设备时演示）
# ============================================================
SIM_PRESETS = {
    "core-sw-01": {
        "health": {"status": "up", "cpu": 92, "mem": 63,
                   "sysDescr": "Huawei S5735 Core Switch (simulated)",
                   "uptime": "128 days, 6:12:41"},
        "interfaces": {
            "GE0/0/1": {"oper": "up", "in_err": 0, "out_err": 0},
            "GE0/0/2": {"oper": "up", "in_err": 1543, "out_err": 27},
        },
    },
    "core-rtr-02": {
        "health": {"status": "up", "cpu": 41, "mem": 38,
                   "sysDescr": "Cisco IOS XR Router (simulated)",
                   "uptime": "365 days, 1:02:11"},
        "interfaces": {
            "Gi0/1": {"oper": "up", "in_err": 2, "out_err": 0},
            "Gi0/2": {"oper": "down", "in_err": 0, "out_err": 0},
        },
    },
    "agg-sw-03": {
        "health": {"status": "down", "cpu": 0, "mem": 0,
                   "sysDescr": "ZTE ZXR10 Aggregation Switch (simulated)",
                   "uptime": "-"},
        "interfaces": {},
    },
}


def _seed(name: str) -> int:
    return abs(hash(name))


def sim_health(name: str):
    if name in SIM_PRESETS:
        return {"device": name, **SIM_PRESETS[name]["health"]}
    h = _seed(name)
    status = "down" if h % 7 == 0 else "up"
    return {
        "device": name, "status": status,
        "cpu": 0 if status == "down" else 25 + h % 70,
        "mem": 0 if status == "down" else 20 + h % 65,
        "sysDescr": f"Simulated Network Device {name}",
        "uptime": "-" if status == "down" else f"{h % 400} days",
    }


def sim_interfaces(name: str):
    if name in SIM_PRESETS:
        return {"device": name, "interfaces": SIM_PRESETS[name]["interfaces"]}
    h = _seed(name)
    if h % 7 == 0:
        return {"device": name, "error": "设备不可达，无法采集接口信息"}
    ifs = {
        "GE0/0/1": {"oper": "up", "in_err": h % 5, "out_err": 0},
        "GE0/0/2": {"oper": "up", "in_err": (h % 900) + 20, "out_err": h % 15},
    }
    if h % 5 == 0:
        ifs["GE0/0/3"] = {"oper": "down", "in_err": 0, "out_err": 0}
    return {"device": name, "interfaces": ifs}


def real_health(device):
    """真实设备：读 sysDescr / sysName / sysUpTime 判断可达性"""
    try:
        descr = snmp_get(device["ip"], OID["sysDescr"], device["community"], device["version"])
        name = snmp_get(device["ip"], OID["sysName"], device["community"], device["version"])
        uptime = snmp_get(device["ip"], OID["sysUpTime"], device["community"], device["version"])
        return {"device": device["name"], "status": "up", "cpu": None, "mem": None,
                "sysName": str(name), "sysDescr": str(descr)[:120], "uptime": str(uptime)}
    except Exception as e:
        return {"device": device["name"], "status": "down", "error": str(e)}


def _walk_map(ip, oid, community, version):
    return {o.split(".")[-1]: v for o, v in snmp_walk(ip, oid, community, version)}


def real_interfaces(device):
    """真实设备：walk ifDescr / ifOperStatus / ifInErrors / ifOutErrors 并按索引对齐"""
    try:
        ip, comm, ver = device["ip"], device["community"], device["version"]
        descrs = _walk_map(ip, OID["ifDescr"], comm, ver)
        opers = _walk_map(ip, OID["ifOperStatus"], comm, ver)
        in_err = _walk_map(ip, OID["ifInErrors"], comm, ver)
        out_err = _walk_map(ip, OID["ifOutErrors"], comm, ver)
        interfaces = {}
        for idx, name in descrs.items():
            oper_code = opers.get(idx, "3")
            oper = "up" if oper_code == "1" else ("down" if oper_code == "2" else "unknown")
            interfaces[str(name)] = {
                "oper": oper,
                "in_err": int(in_err.get(idx, 0) or 0),
                "out_err": int(out_err.get(idx, 0) or 0),
            }
        return {"device": device["name"], "interfaces": interfaces}
    except Exception as e:
        return {"device": device["name"], "error": str(e)}
