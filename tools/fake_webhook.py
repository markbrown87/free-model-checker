#!/usr/bin/env python3
"""Fake Discord webhook for local verification of free-model-checker.

Every POSTed JSON payload is printed to stdout as one readable block and
answered with HTTP 204 (the status Discord returns for a delivered webhook).
GET answers 200 as a health check. Pass ``--log PATH`` to also append one raw
JSON payload per line (JSONL) for scripted assertions.

Standard library only; no third-party dependencies.

Examples:
    python tools/fake_webhook.py                     # 0.0.0.0:9090
    python tools/fake_webhook.py --host 127.0.0.1 --port 9090 --log /tmp/hooks.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _PayloadLog:
    """Append-only JSONL writer for captured webhook payloads."""

    def __init__(self, path: Optional[str]) -> None:
        self.path = path

    def append(self, body: bytes) -> None:
        if not self.path:
            return
        text = body.decode("utf-8", "replace").strip()
        try:
            line = json.dumps(
                json.loads(text), ensure_ascii=False, separators=(",", ":")
            )
        except json.JSONDecodeError:
            # Not JSON: collapse to a single line so the file stays JSONL-shaped.
            line = " ".join(text.split())
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeWebhook/1.0"
    payload_log = _PayloadLog(None)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._respond(200, b"ok\n", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server naming
        self._respond(200, b"", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        self._print_payload(body)
        self.payload_log.append(body)
        # Discord answers 204 No Content for accepted webhook deliveries.
        self._respond(204, b"", None)

    # -- helpers ---------------------------------------------------------- #

    def _print_payload(self, body: bytes) -> None:
        raw = body.decode("utf-8", "replace")
        print(
            f"\n=== {_now()} POST {self.path} ({len(body)} bytes) ===",
            flush=True,
        )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            print(raw, flush=True)
        else:
            print(json.dumps(parsed, ensure_ascii=False, indent=2), flush=True)
        print(flush=True)

    def _respond(self, status: int, body: bytes, content_type: Optional[str]) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Silence the default per-request access log; payload blocks are enough.
        return


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", default="0.0.0.0", help="bind host (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=9090, help="bind port (default: 9090)"
    )
    parser.add_argument(
        "--log",
        metavar="PATH",
        default=None,
        help="also append one raw JSON payload per line to PATH (JSONL)",
    )
    args = parser.parse_args(argv)

    Handler.payload_log = _PayloadLog(args.log)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fake webhook listening on http://{args.host}:{args.port}/", flush=True)
    if args.log:
        print(f"appending payloads to {args.log}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nfake webhook stopped", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
