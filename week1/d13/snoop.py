"""把 getCmd 返回的四元组原样打出来 —— 看 pysnmp 到底给了我们什么。"""
import sys

from pysnmp.hlapi import CommunityData, ContextData, ObjectIdentity, ObjectType
from pysnmp.hlapi import SnmpEngine, UdpTransportTarget, getCmd

host, port, oid = sys.argv[1], int(sys.argv[2]), sys.argv[3]
community = sys.argv[4] if len(sys.argv) > 4 else "public"

ind, st, idx, vb = getCmd(
    SnmpEngine(),
    CommunityData(community),
    UdpTransportTarget((host, port), timeout=0.4, retries=0),
    ContextData(),
    ObjectType(ObjectIdentity(oid)),
)
print("indication :", type(ind).__name__, "|", ind)
print("status     :", type(st).__name__, "|", st, "| int =", int(st))
print("var_binds  :", len(vb), "个")
if vb:
    print("value      :", type(vb[0][1]).__name__, "|", vb[0][1].prettyPrint())