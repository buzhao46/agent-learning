# -*- coding: utf-8 -*-
"""
网络运维 Agent Demo —— HTTP 服务
零第三方 Web 框架依赖（stdlib http.server），仅 SNMP 依赖 pysnmp。

接口：
  GET  /                     Vue 控制台
  GET  /api/health           运行环境 / pysnmp 可用性 / 预置设备
  GET  /api/snmp/test?ip=..  测试真实设备 SNMP 连通性
  POST /api/run              执行 Agent，返回完整 trace
"""

import json
import mimetypes
import os
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from agent import run_agent, TOOL_SPECS
from snmp_client import HAS_PYSNMP, test_connectivity

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

DEFAULT_PRESETS = [
    {"name": "core-sw-01", "ip": "10.10.0.1", "community": "public", "version": "2c", "simulated": True},
    {"name": "core-rtr-02", "ip": "10.10.0.2", "community": "public", "version": "2c", "simulated": True},
    {"name": "agg-sw-03", "ip": "10.10.0.3", "community": "public", "version": "2c", "simulated": True},
]


class Handler(BaseHTTPRequestHandler):
    server_version = "SnmpAgentDemo/1.0"

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path):
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".js"):
            ctype = "application/javascript"
        elif path.endswith(".html"):
            ctype = "text/html"
        elif path.endswith(".css"):
            ctype = "text/css"
        charset = "; charset=utf-8" if ctype.startswith("text") or ctype == "application/javascript" else ""
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + charset)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/api/health":
            return self._json({"ok": True, "pysnmp": HAS_PYSNMP,
                               "presets": DEFAULT_PRESETS, "tools": TOOL_SPECS})
        if p == "/api/snmp/test":
            q = parse_qs(u.query)
            ip = (q.get("ip") or [""])[0]
            community = (q.get("community") or ["public"])[0]
            version = (q.get("version") or ["2c"])[0]
            if not ip:
                return self._json({"ok": False, "error": "缺少 ip 参数"}, 400)
            try:
                return self._json({"ok": True, **test_connectivity(ip, community, version)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if p in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"))
        if p.startswith("/static/"):
            safe = os.path.normpath(os.path.join(STATIC, p[len("/static/"):]))
            if safe.startswith(STATIC):
                return self._file(safe)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/run":
            return self._json({"error": "not found"}, 404)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._json({"error": "invalid json"}, 400)

        task = payload.get("task") or "巡检所有设备，找出异常并给出处理建议。"
        mode = payload.get("mode", "simulate")
        if mode == "real":
            devices = [{**d, "simulated": False} for d in (payload.get("devices") or []) if d.get("ip")]
        else:
            devices = DEFAULT_PRESETS

        if not devices:
            return self._json({"error": "没有可用设备（真实模式请先添加至少一台带 IP 的设备）"}, 400)

        # 设备名去重，避免后写覆盖
        registry = {}
        for d in devices:
            name = d.get("name") or d.get("ip")
            registry[name] = {**d, "name": name}

        try:
            result = run_agent(task, registry)
        except Exception as e:
            return self._json({"error": f"agent 运行失败: {e}"}, 500)
        return self._json(result)

    def log_message(self, fmt, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"SNMP Agent Demo -> http://127.0.0.1:{port}")
    print(f"pysnmp available: {HAS_PYSNMP}")
    server.serve_forever()


if __name__ == "__main__":
    main()
