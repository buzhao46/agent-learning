"""D19：ack 闭环 HTTP 服务 —— 让「在手机上点一下就确认」成为现实。

    python ack_server.py --state out/alert_state.json --groups groups.json \
        --host 0.0.0.0 --port 8731 --secret my-secret

它在 D18 的 ack 流程上补了「最后一公里」：
    alert.py 发通知时，payload 里带一条签名确认链接（见 notify.py + acktoken.py）；
    运维在聊天里点链接 → 打到本服务的 /ack → 校验签名后调用 ack.apply_ack 写回状态；
    下次 alert.py 跑，被确认的设备不再升级（D18 的逻辑不变）。

路由：
    GET /                      看板（HTML，复用 board.render_html）
    GET /api/board             JSON 看板
    GET /ack?device=X&sig=S&by=Y   确认单台
    GET /ack?group=G&sig=S&by=Y    确认整组

安全：链接签名用 --secret 校验，没有密钥造不出合法 sig，没法随便消音别人告警。
本服务只负责「确认」，不开放「取消确认 / 改状态」——确认是不可逆的人为动作。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import acktoken
import board
from ack import apply_ack
from state import load_groups, load_state, save_state

SECRET_ENV = "ACK_SECRET"


class AckHandler(BaseHTTPRequestHandler):
    # 让日志安静一点
    def log_message(self, *args):  # noqa: D401
        pass

    def _send(self, body: str, code: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        if parsed.path == "/ack":
            self._handle_ack(q)
        elif parsed.path == "/api/board":
            self._serve_board_json()
        else:
            self._serve_board_html()

    def _serve_board_html(self) -> None:
        state, _ = load_state(self.server.state_path, now=datetime.now())
        groups = load_groups(self.server.groups_path)
        rows, grows, summary = board.collect(state, groups, datetime.now())
        self._send(board.render_html(rows, grows, summary))

    def _serve_board_json(self) -> None:
        state, _ = load_state(self.server.state_path, now=datetime.now())
        groups = load_groups(self.server.groups_path)
        rows, grows, summary = board.collect(state, groups, datetime.now())
        import json
        self._send(json.dumps({"devices": rows, "groups": grows, "summary": summary},
                              ensure_ascii=False), content_type="application/json; charset=utf-8")

    def _handle_ack(self, q: dict) -> None:
        device = (q.get("device") or [""])[0]
        group = (q.get("group") or [""])[0]
        sig = (q.get("sig") or [None])[0]
        by = (q.get("by") or ["未知"])[0]

        if device:
            if not acktoken.verify_device(self.server.secret, device, sig):
                self._send("<h1>403</h1><p>签名无效，拒绝确认。</p>", code=403)
                return
            targets = {device}
        elif group:
            if not acktoken.verify_group(self.server.secret, group, sig):
                self._send("<h1>403</h1><p>签名无效，拒绝确认。</p>", code=403)
                return
            groups = load_groups(self.server.groups_path)
            targets = {m for g in groups.get("groups", []) if g.get("name") == group
                       for m in g.get("members", [])}
        else:
            self._send("<h1>400</h1><p>缺少 device 或 group。</p>", code=400)
            return

        if not targets:
            self._send("<h1>400</h1><p>目标为空，可能分组里没有设备。</p>", code=400)
            return

        now = datetime.now()
        state, _ = load_state(self.server.state_path, now=now)
        n = apply_ack(state, targets, by, "", now)
        save_state(state, self.server.state_path, now=now)
        target_txt = device or group
        self._send(
            f"<h1>已确认</h1><p>已确认 <b>{n}</b> 台（{target_txt}）。</p>"
            f"<p><a href='/'>返回看板</a></p>"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="D19：ack 闭环 HTTP 服务")
    parser.add_argument("--state", default="out/alert_state.json", help="告警状态文件")
    parser.add_argument("--groups", help="分组拓扑 JSON")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8731)
    parser.add_argument("--secret", default=acktoken.DEFAULT_SECRET, help="ack 链接签名密钥")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), AckHandler)
    httpd.state_path = args.state
    httpd.groups_path = args.groups
    httpd.secret = args.secret
    print(f"ack 闭环服务已启动： http://{args.host}:{args.port}/  （secret={'默认' if args.secret == acktoken.DEFAULT_SECRET else '已设置'}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
