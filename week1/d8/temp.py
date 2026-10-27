from pysnmp.hlapi import *

eng = SnmpEngine()
r = getCmd(eng, CommunityData('public'), UdpTransportTarget(('127.0.0.1', 1161), timeout=1.0, retries=0), ContextData(), ObjectType(ObjectIdentity('1.3.6.1.2.1.1.1.0')))
print('整体类型:', type(r))
print('有', len(r), '个元素')
for i, x in enumerate(r):
    print(f'  [{i}]', type(x).__name__, '=', x)

eng = SnmpEngine()
_, _, _, vbs = getCmd(eng, CommunityData('public'), UdpTransportTarget(('127.0.0.1', 1161), timeout=1.0, retries=0), ContextData(), ObjectType(ObjectIdentity('1.3.6.1.2.1.1.1.0')))
name, value = vbs[0]
print('OID =', name)
print('值的类型 =', type(value).__name__)
print('prettyPrint() =', repr(value.prettyPrint()))