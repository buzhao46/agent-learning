"""snmp-insight: SNMP 网络巡检采集器（list / status 子命令）。

本包同时支持两种运行方式：
- 安装后作为命令：``snmpcheck list`` / ``snmpcheck status``
- 开发期作为脚本：``python -m snmpcheck.snmpcheck``（在 week1 目录下）
模块内的兄弟导入使用 try/except，两种上下文都能解析。
"""
