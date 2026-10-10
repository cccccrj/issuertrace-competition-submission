#!/usr/bin/env python3
"""A webhook endpoint for demonstrating delivery without an internet connection.

The platform's push feature is real: it signs a body and posts it over HTTP. But
demonstrating that on a venue's wifi, to somebody else's endpoint, is not a
demonstration anybody can rely on. This is the other end of the wire, on
loopback, in the standard library.

It verifies the HMAC signature the platform sends and appends every accepted
delivery to a JSONL file, so what arrives can be read back and compared with
what the outbox says was sent. A receiver that accepted anything would prove
only that a request was made; one that checks the signature proves the request
was ours and arrived intact.

    NOTIFY_WEBHOOK_SECRET=demo-secret python3 scripts/webhook_receiver.py
    # then point a subscription at http://127.0.0.1:8787/hook with
    # secret_ref = NOTIFY_WEBHOOK_SECRET and dry-run off

Rejects with 401 when the signature is missing or wrong, which is what makes a
green delivery in the outbox mean something.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

RECEIPTS = Path(__file__).resolve().parents[1] / "data" / "state" / "webhook_receipts.jsonl"


class Handler(BaseHTTPRequestHandler):
    secret: str | None = None
    receipts: Path = RECEIPTS

    def _reply(self, code: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)

        if self.secret:
            expected = "sha256=" + hmac.new(
                self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            given = self.headers.get("X-Signature", "")
            # compare_digest, not ==: a timing-safe comparison costs nothing here
            # and the habit is the point.
            if not hmac.compare_digest(expected, given):
                self._reply(401, {"ok": False, "error": "签名不匹配"})
                return

        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            self._reply(400, {"ok": False, "error": "不是合法 JSON"})
            return

        self.receipts.parent.mkdir(parents=True, exist_ok=True)
        with self.receipts.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "path": self.path,
                "signed": bool(self.secret),
                "payload": payload,
            }, ensure_ascii=False) + "\n")

        self._reply(200, {"ok": True, "alert_id": payload.get("alert_id")})

    def log_message(self, fmt: str, *args) -> None:
        # One line per delivery, on stdout, so a demonstration can show the
        # receiving side without a second terminal full of noise.
        print(f"[receiver] {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--secret-env", default="NOTIFY_WEBHOOK_SECRET",
                        help="环境变量名，其值用于校验签名；未设置则不校验")
    parser.add_argument("--receipts", type=Path, default=RECEIPTS)
    args = parser.parse_args()

    Handler.secret = os.environ.get(args.secret_env)
    Handler.receipts = args.receipts
    if not Handler.secret:
        print(f"[receiver] {args.secret_env} 未设置：不校验签名（仅供本机演示）")

    server = HTTPServer((args.host, args.port), Handler)
    print(f"[receiver] 监听 http://{args.host}:{args.port}/  收件写入 {args.receipts}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[receiver] 停止")


if __name__ == "__main__":
    main()
