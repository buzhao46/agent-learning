"""一台「假交换机」——真的 UDP、真的 BER 编码、真的 SNMPv2c 报文。

D9 那台只会应答 GET（单个 OID）。D10 要 WALK 接口表，所以这台多了两样东西：

    1. 一张 ifTable（4 个接口），真实设备的标准 OID 布局
    2. GETNEXT (0xA1) 与 GETBULK (0xA5) 的应答能力 —— WALK 就靠它

    python fake_agent.py                      # 4 个接口全都在
    python fake_agent.py --miss-status        # ifIndex=3 的 ifOperStatus 缺失（设备 bug）
    python fake_agent.py --loop               # GETNEXT 表末尾原地打转（设备 bug）
    python fake_agent.py --port 1162 --name agg-sw-03

为什么要有 `--miss-status` 和 `--loop` 这两个开关：它们是**真实网络里最常见的两类
WALK 事故**，但在本机造不出来。

    --miss-status  某些设备固件对个别接口不返回 ifOperStatus（接口刚创建、
                   或者处于 notPresent 状态）。如果代码用 zip() 合并两张表，
                   从这一行开始全部错位 —— 而结果看起来完全正常。
    --loop         少数老固件在表末尾回一个「不比请求大」的 OID。pysnmp 默认会
                   把它当成异常丢掉，但如果你设了 ignoreNonIncreasingOid=True
                   又没给自己留行数上限，程序就会永远转下去。

这个文件**已经写好了，你不需要改，也不需要看懂**（BER 编解码是另一个话题）。
"""  # noqa: DOC001

from __future__ import annotations

import argparse
import socket

# ---------------------------------------------------------------- 设备「人格」
DESCR = "Huawei S5735-L48T4X-A1, VRP (R) software, Version 5.170"
NAME = "core-sw-01"
UPTIME_TICKS = 12345678  # 1/100 秒 → 约 34 小时
BAD_UPTIME = False  # True 时 sysUpTime 回字符串 "N/A"（模拟脏值）
MISS_STATUS = False  # True 时 ifIndex=3 没有 ifOperStatus 行
LOOP_BUG = False  # True 时 GETNEXT 表末尾原地打转
MANY_PORTS = False  # True 时变成 50 个接口（验证行数上限用）

OID_SYSDESCR = "1.3.6.1.2.1.1.1.0"
OID_SYSUPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYSNAME = "1.3.6.1.2.1.1.5.0"

# WALK 的起点（列 OID，不带行号）
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_TYPE = "1.3.6.1.2.1.2.2.1.3"
OID_IF_OPER = "1.3.6.1.2.1.2.2.1.8"
OID_IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"

# 4 个接口。第 3 个刻意用中文名 —— 国内局点改 ifDescr 是常事，
# 而中文字符在终端里占两格，表格对齐会立刻出问题。
BASE_INTERFACES = [
    (1, "GE0/0/1", 6, 1, 1284993201),  # up
    (2, "GE0/0/2", 6, 1, 88120442),  # up
    (3, "上联口-核心", 6, 2, 4294967100),  # down（上联断了）
    (4, "XGE0/0/4", 6, 1, 512004),  # up
]


def interfaces() -> list[tuple[int, str, int, int, int]]:
    """这台设备有哪些接口。--many 时膨胀成 50 个，用来验证行数上限。"""
    if MANY_PORTS:
        return [(i, f"GE0/0/{i}", 6, 1, 1000 * i) for i in range(1, 51)]
    return BASE_INTERFACES

TAG_INT = 0x02
TAG_OCTETS = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQ = 0x30
TAG_IPADDR = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42
TAG_TIMETICKS = 0x43
TAG_NO_SUCH_OBJECT = 0x80
TAG_NO_SUCH_INSTANCE = 0x81
TAG_END_OF_MIB_VIEW = 0x82
TAG_GET_REQUEST = 0xA0
TAG_GET_NEXT_REQUEST = 0xA1
TAG_GET_RESPONSE = 0xA2
TAG_GET_BULK_REQUEST = 0xA5


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


def _encode_unsigned(tag: int, number: int) -> bytes:
    """Counter32 / Gauge32 / TimeTicks 都是无符号，但 BER 的 INTEGER 家族天生有符号。

    最高位恰好为 1 时必须补一个 0x00，否则对方会解成负数，整包直接丢弃。
    """
    width = max(1, (number.bit_length() + 7) // 8)
    if number > 0 and number.bit_length() % 8 == 0:
        width += 1
    return _encode_tlv(tag, number.to_bytes(width, "big"))


def _encode_int(number: int) -> bytes:
    """有符号 INTEGER：负数按补码编码，正数最高位为 1 时补 0x00。"""
    width = 1
    while not -(1 << (8 * width - 1)) <= number < (1 << (8 * width - 1)):
        width += 1
    return _encode_tlv(TAG_INT, number.to_bytes(width, "big", signed=True))


def _encode_oid(oid: str) -> bytes:
    """把 "1.3.6.1..." 编成 BER 的 OID（含 tag 和长度）。"""
    parts = [int(x) for x in oid.split(".")]
    body = bytearray([parts[0] * 40 + parts[1]])
    for number in parts[2:]:
        chunk = [number & 0x7F]
        number >>= 7
        while number:
            chunk.append((number & 0x7F) | 0x80)
            number >>= 7
        body.extend(reversed(chunk))
    return _encode_tlv(TAG_OID, bytes(body))


# ---------------------------------------------------------------- MIB 表
def mib() -> list[tuple[tuple[int, ...], str, bytes]]:
    """组装这台设备支持的整套 OID：[(OID 元组, OID 字符串, 已编码的 value TLV), ...]

    按 OID 数值升序 —— GETNEXT 就是在有序表里找「下一个更大的」，和真实设备一样。
    """
    rows: list[tuple[str, bytes]] = [
        (OID_SYSDESCR, _encode_tlv(TAG_OCTETS, DESCR.encode())),
        (
            OID_SYSUPTIME,
            _encode_tlv(TAG_OCTETS, b"N/A")
            if BAD_UPTIME
            else _encode_unsigned(TAG_TIMETICKS, UPTIME_TICKS),
        ),
        (OID_SYSNAME, _encode_tlv(TAG_OCTETS, NAME.encode())),
        ("1.3.6.1.2.1.2.1.0", _encode_int(len(interfaces()))),  # ifNumber
    ]

    for index, descr, if_type, oper, octets in interfaces():
        rows.append((f"1.3.6.1.2.1.2.2.1.1.{index}", _encode_int(index)))  # ifIndex
        rows.append((f"{OID_IF_DESCR}.{index}", _encode_tlv(TAG_OCTETS, descr.encode())))
        rows.append((f"{OID_IF_TYPE}.{index}", _encode_int(if_type)))
        # --miss-status：设备就是不给第 3 个接口的 ifOperStatus（真实固件 bug）
        if not (MISS_STATUS and index == 3):
            rows.append((f"{OID_IF_OPER}.{index}", _encode_int(oper)))
        rows.append((f"{OID_IF_IN_OCTETS}.{index}", _encode_unsigned(TAG_COUNTER32, octets)))

    # 后面两张表是「诱饵」：如果 WALK 不限制在 ifDescr 的子树里，
    # 就会一路走进来，结果里凭空多出一堆 IP 地址。
    rows.append(("1.3.6.1.2.1.4.20.1.1.10.20.30.1", _encode_tlv(TAG_IPADDR, bytes([10, 20, 30, 1]))))
    rows.append(("1.3.6.1.2.1.4.20.1.2.10.20.30.1", _encode_int(1)))
    rows.append(("1.3.6.1.2.1.4.20.1.1.192.168.1.1", _encode_tlv(TAG_IPADDR, bytes([192, 168, 1, 1]))))
    rows.append(("1.3.6.1.2.1.4.20.1.2.192.168.1.1", _encode_int(2)))

    table = [(tuple(int(x) for x in oid.split(".")), oid, tlv) for oid, tlv in rows]
    table.sort(key=lambda item: item[0])
    return table


def lookup(oid: str) -> bytes | None:
    """GET 用：精确匹配。找不到就 None（外面回 noSuchObject）。"""
    want = tuple(int(x) for x in oid.split("."))
    for key, _, tlv in mib():
        if key == want:
            return tlv
    return None


def next_oid(oid: str) -> tuple[str, bytes] | None:
    """GETNEXT 用：返回「严格大于 oid」的第一个 (OID, value TLV)；到末尾返回 None。"""
    cur = tuple(int(x) for x in oid.split("."))
    for key, text, tlv in mib():
        if key > cur:
            return text, tlv
    return None


# ---------------------------------------------------------------- 报文解析
def _slice(data: bytes, i: int) -> tuple[bytes, bytes, int]:
    """返回 (这个 TLV 的完整字节, value 字节, 下一个 TLV 偏移)。

    三个东西各有用途：完整字节用来原样回抄，value 字节用来读数值，
    下一个偏移用来继续往后走。
    """
    _, value, _, nxt = _tlv(data, i)
    return data[i:nxt], value, nxt


def parse_request(data: bytes) -> dict[str, object]:
    """拆一条 SNMPv2c 请求（GET / GETNEXT / GETBULK 都认）。"""
    tag, _, vstart, _ = _tlv(data, 0)
    if tag != TAG_SEQ:
        raise ValueError(f"外层不是 SEQUENCE，tag=0x{tag:02X}")

    i = vstart
    version_raw, _, i = _slice(data, i)

    j = i
    _, comm_val, _, i = _tlv(data, j)
    community_raw = data[j:i]

    j = i
    ptag, _, pvstart, _ = _tlv(data, j)
    if ptag not in (TAG_GET_REQUEST, TAG_GET_NEXT_REQUEST, TAG_GET_BULK_REQUEST):
        raise ValueError(f"只支持 GET/GETNEXT/GETBULK，收到 PDU tag=0x{ptag:02X}")

    k = pvstart
    reqid_raw, _, k = _slice(data, k)
    # GET 时这两格是 error-status / error-index；
    # GETBULK 时它们改叫 non-repeaters / max-repetitions —— 同样的位置，不同的含义。
    slot1_raw, slot1_val, k = _slice(data, k)
    slot2_raw, slot2_val, k = _slice(data, k)

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
        "pdu_tag": ptag,
        "reqid_raw": reqid_raw,
        "slot1_raw": slot1_raw,
        "slot1": int.from_bytes(slot1_val, "big") if slot1_val else 0,
        "slot2_raw": slot2_raw,
        "slot2": int.from_bytes(slot2_val, "big") if slot2_val else 0,
        "oids": oids,
    }


# ---------------------------------------------------------------- 报文组装
def _answer(oid: str, pdu_tag: int) -> tuple[str, bytes]:
    """算出一个 varbind 该回什么，返回 (回显的 OID, value TLV)。"""
    if pdu_tag == TAG_GET_REQUEST:
        value = lookup(oid)
        return oid, value if value is not None else _encode_tlv(TAG_NO_SUCH_OBJECT, b"")

    found = next_oid(oid)
    if found is None:
        # 走到 MIB 末尾：协议规定回 endOfMibView，并且 OID 原样回显。
        return oid, _encode_tlv(TAG_END_OF_MIB_VIEW, b"")
    return found


def build_response(request: bytes) -> bytes:
    """把请求翻成 GET-RESPONSE（version / community / request-id 原样回抄）。"""
    msg = parse_request(request)
    pdu_tag = msg["pdu_tag"]
    oids = msg["oids"]

    varbinds: list[bytes] = []
    for oid, _oid_tlv in oids:
        current = oid
        # GETBULK 一次要 max-repetitions 行；GET / GETNEXT 只要 1 行
        repeats = msg["slot2"] if pdu_tag == TAG_GET_BULK_REQUEST else 1
        repeats = max(1, min(int(repeats), 16))  # 别让假的把包撑爆
        for _ in range(repeats):
            echoed, value = _answer(current, pdu_tag)
            varbinds.append(_encode_tlv(TAG_SEQ, _encode_oid(echoed) + value))
            if echoed == current:  # 走到尽头（endOfMibView），不必再重复
                break
            current = echoed

    # --loop：模拟「表末尾回一个不比请求大的 OID」这个固件 bug。
    # 表现：永远回同一条，客户端若不设上限就会一直转。
    if LOOP_BUG and pdu_tag != TAG_GET_REQUEST:
        last = interfaces()[-1][0]
        stuck = f"{OID_IF_DESCR}.{last}"
        varbinds = [
            _encode_tlv(TAG_SEQ, _encode_oid(stuck) + _encode_tlv(TAG_OCTETS, b"LOOP-BUG"))
        ]

    pdu = _encode_tlv(
        TAG_GET_RESPONSE,
        msg["reqid_raw"]
        + msg["slot1_raw"]
        + msg["slot2_raw"]
        + _encode_tlv(TAG_SEQ, b"".join(varbinds)),
    )
    return _encode_tlv(TAG_SEQ, msg["version_raw"] + msg["community_raw"] + pdu)


def serve(host: str, port: int, community: str) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    print(f"假设备已启动：{host}:{port}  name={NAME}  community={community}", flush=True)
    print(f"接口数 {len(interfaces())}  miss_status={MISS_STATUS}  loop_bug={LOOP_BUG}  many={MANY_PORTS}", flush=True)
    print("Ctrl+C 停止。保持这个窗口开着，另开一个窗口跑你的代码。", flush=True)
    while True:
        data, peer = sock.recvfrom(8192)
        try:
            # community 不对就静默丢弃 —— 这正是真实设备的行为：
            # SNMPv2c 认证失败时，客户端看到的是「超时」，不是「community 错」。
            if parse_request(data)["community"] != community:
                continue
            sock.sendto(build_response(data), peer)
        except Exception as exc:  # noqa: BLE001 - 假设备不能因为一个坏包就退出
            print(f"丢弃一个坏包：{type(exc).__name__}: {exc}", flush=True)


def main() -> int:
    global NAME, BAD_UPTIME, MISS_STATUS, LOOP_BUG, MANY_PORTS
    parser = argparse.ArgumentParser(description="最小 SNMPv2c 应答器（假设备，支持 WALK）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1161)
    parser.add_argument("--community", default="public")
    parser.add_argument("--name", default=NAME)
    parser.add_argument("--uptime-bad", action="store_true", help="sysUpTime 回 N/A（脏值）")
    parser.add_argument("--miss-status", action="store_true", help="ifIndex=3 缺 ifOperStatus")
    parser.add_argument("--loop", action="store_true", help="GETNEXT 表末尾原地打转")
    parser.add_argument("--many", action="store_true", help="膨胀成 50 个接口")
    args = parser.parse_args()
    NAME = args.name
    BAD_UPTIME = args.uptime_bad
    MISS_STATUS = args.miss_status
    LOOP_BUG = args.loop
    MANY_PORTS = args.many
    serve(args.host, args.port, args.community)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
