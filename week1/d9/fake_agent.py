"""一台「假交换机」——真的 UDP、真的 BER 编码、真的 SNMPv2c 报文。

和 d8/fake_agent.py 是同一个东西，只多了一个开关：

    python fake_agent.py                      # sysUpTime 回正常的 TimeTicks
    python fake_agent.py --uptime-bad         # sysUpTime 回字符串 "N/A"（脏值）
    python fake_agent.py --port 1171 --name agg-sw-03

为什么要加 `--uptime-bad`：D9 要验收「字段脏了怎么办」。真机上这种事天天发生
（设备不支持这个 OID、回 -1、回空串），但在本机造不出来，所以给它一个开关。

这个文件**已经写好了，你不需要改，也不需要看懂**（BER 编码是另一个话题）。
"""  # noqa: DOC001

from __future__ import annotations

import argparse
import socket

# ---------------------------------------------------------------- 设备「人格」
DESCR = "Huawei S5735-L48T4X-A1, VRP (R) software, Version 5.170"
NAME = "core-sw-01"
UPTIME_TICKS = 12345678  # 1/100 秒 → 约 34 小时
BAD_UPTIME = False  # True 时 sysUpTime 回字符串 "N/A"（模拟脏值）

OID_SYSDESCR = "1.3.6.1.2.1.1.1.0"
OID_SYSUPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYSNAME = "1.3.6.1.2.1.1.5.0"

TAG_INT = 0x02
TAG_OCTETS = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQ = 0x30
TAG_TIMETICKS = 0x43
TAG_NO_SUCH_OBJECT = 0x80
TAG_GET_REQUEST = 0xA0
TAG_GET_RESPONSE = 0xA2


# ---------------------------------------------------------------- BER 读
def _len(data: bytes, i: int) -> tuple[int, int]:
    """读长度字段，返回 (长度, 长度字段之后的偏移)。"""
    first = data[i]
    if first < 0x80:
        return first, i + 1
    count = first & 0x7F
    return int.from_bytes(data[i + 1 : i + 1 + count], "big"), i + 1 + count


def _tlv(data: bytes, i: int) -> tuple[int, bytes, int, int]:
    """读一个 TLV。

    返回 (tag, value 原始字节, value 在报文里的起始偏移, 下一个 TLV 的偏移)。
    """
    tag = data[i]
    length, value_start = _len(data, i + 1)
    return tag, data[value_start : value_start + length], value_start, value_start + length


def _decode_oid(raw: bytes) -> str:
    if not raw:
        return ""
    parts = [raw[0] // 40, raw[0] % 40]
    value = 0
    for byte in raw[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(value)
            value = 0
    return ".".join(str(p) for p in parts)


# ---------------------------------------------------------------- BER 写
def _encode_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _encode_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _encode_len(len(value)) + value


def _encode_time_ticks(ticks: int) -> bytes:
    """TimeTicks 是无符号 32 位整数，但 BER 的 INTEGER 家族天生有符号。

    最高位恰好为 1 时（如 12345678 = 0xBC614E），必须补一个 0x00，
    否则对方会解成负数，整包直接丢弃。
    """
    width = max(1, (ticks.bit_length() + 7) // 8)
    if ticks > 0 and ticks.bit_length() % 8 == 0:
        width += 1
    return _encode_tlv(TAG_TIMETICKS, ticks.to_bytes(width, "big"))


def _varbind_value(oid: str) -> bytes:
    """按 OID 决定回什么值；不认识的 OID 回 noSuchObject。"""
    if oid == OID_SYSDESCR:
        return _encode_tlv(TAG_OCTETS, DESCR.encode())
    if oid == OID_SYSNAME:
        return _encode_tlv(TAG_OCTETS, NAME.encode())
    if oid == OID_SYSUPTIME:
        # 脏值模式：回一个字符串而不是 TimeTicks —— 真机上「不支持」的常见形态
        return _encode_tlv(TAG_OCTETS, b"N/A") if BAD_UPTIME else _encode_time_ticks(UPTIME_TICKS)
    return _encode_tlv(TAG_NO_SUCH_OBJECT, b"")


# ---------------------------------------------------------------- 报文解析
def _slice(data: bytes, i: int) -> tuple[bytes, int, int]:
    """返回 (这个 TLV 的完整字节, value 起始偏移, 下一个 TLV 偏移)。"""
    _, _, vstart, nxt = _tlv(data, i)
    return data[i:nxt], vstart, nxt


def parse_get(data: bytes) -> dict[str, object]:
    """拆一条 SNMPv2c GET 报文。只认 GET，够用了。"""
    tag, _, vstart, _ = _tlv(data, 0)
    if tag != TAG_SEQ:
        raise ValueError(f"外层不是 SEQUENCE，tag=0x{tag:02X}")

    i = vstart
    version_raw, _, i = _slice(data, i)

    j = i
    _, comm_val, _, i = _tlv(data, j)
    community_raw = data[j:i]

    j = i
    ptag, _, pvstart, pnext = _tlv(data, j)
    if ptag != TAG_GET_REQUEST:
        raise ValueError(f"只支持 GET，收到 PDU tag=0x{ptag:02X}")

    k = pvstart
    reqid_raw, _, k = _slice(data, k)
    errstatus_raw, _, k = _slice(data, k)
    errindex_raw, _, k = _slice(data, k)

    _, _, vbl_start, vbl_next = _tlv(data, k)
    oids: list[tuple[str, bytes]] = []
    m = vbl_start
    while m < vbl_next:
        _, _, vb_vstart, vb_next = _tlv(data, m)
        n = vb_vstart
        _, oid_val, _, oid_next = _tlv(data, n)
        oids.append((_decode_oid(oid_val), data[n:oid_next]))
        m = vb_next

    return {
        "version_raw": version_raw,
        "community_raw": community_raw,
        "community": comm_val.decode("utf-8", "replace"),
        "reqid_raw": reqid_raw,
        "errstatus_raw": errstatus_raw,
        "errindex_raw": errindex_raw,
        "oids": oids,
        "pdu_next": pnext,
    }


# ---------------------------------------------------------------- 报文组装
def build_response(request: bytes) -> bytes:
    """把 GET 请求翻成 GET 响应（version / community / request-id 原样回抄）。"""
    msg = parse_get(request)
    oids = msg["oids"]

    varbinds = b"".join(
        _encode_tlv(TAG_SEQ, oid_tlv + _varbind_value(oid)) for oid, oid_tlv in oids
    )
    pdu = _encode_tlv(
        TAG_GET_RESPONSE,
        msg["reqid_raw"]
        + msg["errstatus_raw"]
        + msg["errindex_raw"]
        + _encode_tlv(TAG_SEQ, varbinds),
    )
    return _encode_tlv(TAG_SEQ, msg["version_raw"] + msg["community_raw"] + pdu)


def serve(host: str, port: int, community: str) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    print(f"假设备已启动：{host}:{port}  name={NAME}  community={community}", flush=True)
    print("Ctrl+C 停止。保持这个窗口开着，另开一个窗口跑你的代码。", flush=True)
    while True:
        data, peer = sock.recvfrom(4096)
        try:
            # community 不对就静默丢弃 —— 这正是真实设备的行为：
            # SNMPv2c 认证失败时，客户端看到的是「超时」，不是「community 错」。
            if parse_get(data)["community"] != community:
                continue
            sock.sendto(build_response(data), peer)
        except Exception as exc:  # noqa: BLE001 - 假设备不能因为一个坏包就退出
            print(f"丢弃一个坏包：{type(exc).__name__}: {exc}", flush=True)


def main() -> int:
    global NAME, BAD_UPTIME
    parser = argparse.ArgumentParser(description="最小 SNMPv2c 应答器（假设备）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1161)
    parser.add_argument("--community", default="public")
    parser.add_argument("--name", default=NAME)
    parser.add_argument("--uptime-bad", action="store_true", help="sysUpTime 回 N/A（脏值）")
    args = parser.parse_args()
    NAME = args.name
    BAD_UPTIME = args.uptime_bad
    serve(args.host, args.port, args.community)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
