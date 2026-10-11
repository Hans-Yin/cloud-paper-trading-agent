#!/usr/bin/env python3
"""Health endpoint and native Codex OAuth callback relay; no financial routes."""
from __future__ import annotations

import http.client
import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import runner


def prepare_codex(persistent):
    folder = persistent / "codex"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = Path.home() / ".codex"
    if target.is_symlink():
        if target.resolve() != folder.resolve():
            raise runner.CloudError("UNEXPECTED_CREDENTIAL_DIRECTORY")
    elif target.exists():
        raise runner.CloudError("EXISTING_CREDENTIAL_DIRECTORY_REVIEW_REQUIRED")
    else:
        target.symlink_to(folder, target_is_directory=True)
    source = "mcp_oauth_callback_port = 5555\n" + (runner.SOURCE / "cloud" / "codex-config.toml").read_text()
    external = os.environ.get("RENDER_EXTERNAL_URL", "")
    if external:
        parsed = urlsplit(external)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".onrender.com") or parsed.path not in {"", "/"}:
            raise runner.CloudError("UNEXPECTED_CALLBACK_ORIGIN")
        source = ('mcp_oauth_callback_url = '
                  + json.dumps(external.rstrip("/") + "/callback") + "\n" + source)
    (folder / "config.toml").write_text(source)
    (folder / "config.toml").chmod(0o600)
    runner.validate_codex_config(folder / "config.toml")
    return folder / "config.toml"


def bind_host():
    value = os.environ.get("CLOUD_BIND_HOST", "0.0.0.0")
    if value not in {"127.0.0.1", "0.0.0.0"}:
        raise runner.CloudError("INVALID_LISTEN_ADDRESS")
    return value


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # Callback URLs carry OAuth codes: never log them.
        pass

    def reply(self, code, data, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        route = urlsplit(self.path).path
        if route == "/healthz":
            self.reply(200, b'{"service":"ai-short-term-paper","process_running":true}')
        elif route == "/callback" and len(self.path) <= 8192:
            connection = http.client.HTTPConnection("127.0.0.1", 5555, timeout=10)
            try:
                connection.request("GET", self.path, headers={"Host": "127.0.0.1:5555"})
                response = connection.getresponse()
                body = response.read(128000)
                content_type = response.getheader("Content-Type", "text/html; charset=utf-8")
                self.reply(response.status, body, content_type)
            except (OSError, http.client.HTTPException):
                self.reply(503, b'{"error":"No active login callback; start native MCP login in the service shell."}')
            finally:
                connection.close()
        else:
            self.reply(404, b'{"error":"not_found"}')


def main():
    persistent = Path(os.environ.get("PAPER_CLOUD_STORAGE", "/var/data"))
    root = persistent / "system"
    codex_config = prepare_codex(persistent)
    thread = threading.Thread(target=runner.supervise, args=(root, persistent, codex_config), daemon=True)
    thread.start()
    server = ThreadingHTTPServer((bind_host(), int(os.environ.get("PORT", "10000"))), Handler)

    def stop(*_):
        runner.STOP.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        runner.STOP.set()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
