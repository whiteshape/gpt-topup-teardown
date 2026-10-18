"""Local gateway for a region-locked site.

Why this exists
---------------
Some sites answer a mainland-CN IP with a "region not supported" page, while the
machine already has a working HTTP proxy whose exit node sits in a supported
region. If the browser you must use (for example an embedded review-pane
browser) ignores the system proxy, this gateway bridges the gap: it listens on
localhost and replays each request upstream through the proxy.

Usage
-----
    python local_gateway.py --upstream https://example.com --port 8848
    # then open http://127.0.0.1:8848/

It rewrites Set-Cookie (drops Domain/Secure, forces SameSite=Lax) and Location
so cookies and redirects survive the local origin.

It deliberately shells out to curl instead of using Python's TLS stack:
Cloudflare challenges Python's TLS fingerprint but accepts curl's.
"""

from __future__ import annotations

import argparse
import http.server
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

UPSTREAM = "https://example.com"
UPSTREAM_HOST = "example.com"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

DROP_RESPONSE_HEADERS = {
    "content-security-policy",
    "content-security-policy-report-only",
    "strict-transport-security",
    "report-to",
    "nel",
    "alt-svc",
    "content-encoding",
    "content-length",
}

STATE = {
    "proxy": "127.0.0.1:7897",
    "listen": ("127.0.0.1", 8848),
    "curl": shutil.which("curl") or "curl",
}

COOKIE_DOMAIN = re.compile(r";\s*domain=[^;]*", re.I)
COOKIE_SECURE = re.compile(r";\s*secure\b", re.I)
COOKIE_SAMESITE_NONE = re.compile(r";\s*samesite=none\b", re.I)


def rewrite_set_cookie(value: str) -> str:
    value = COOKIE_DOMAIN.sub("", value)
    value = COOKIE_SECURE.sub("", value)
    value = COOKIE_SAMESITE_NONE.sub("; SameSite=Lax", value)
    if "samesite" not in value.lower():
        value += "; SameSite=Lax"
    return value


def parse_header_blocks(text: str):
    """Return [(status, reason, [(name, value), ...]), ...] for every block."""
    blocks = []
    current = None
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("HTTP/"):
            if current:
                blocks.append(current)
            parts = line.split(" ", 2)
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            current = [status, parts[2] if len(parts) > 2 else "", []]
        elif line == "":
            continue
        elif current is not None and ":" in line:
            name, _, value = line.partition(":")
            current[2].append((name.strip(), value.strip()))
    if current:
        blocks.append(current)
    return blocks


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalGateway/1.0"

    def _local_origin(self) -> str:
        host = self.headers.get("Host") or "%s:%d" % STATE["listen"]
        return "http://" + host

    def _read_body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)
        length = self.headers.get("Content-Length")
        if length:
            return self.rfile.read(int(length))
        return None

    def _forward(self) -> None:
        body = self._read_body()
        path = self.path
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urllib.parse.urlsplit(path)
            path = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")

        work = tempfile.mkdtemp(prefix="gw-")
        hdr_path = os.path.join(work, "headers.txt")
        body_path = os.path.join(work, "body.bin")

        cmd = [
            STATE["curl"],
            "--silent",
            "--show-error",
            "--compressed",
            "--max-time",
            "120",
            "--proxy",
            STATE["proxy"],
            "--request",
            self.command,
            "--dump-header",
            hdr_path,
            "--output",
            body_path,
        ]

        for key, value in self.headers.items():
            low = key.lower()
            if low in HOP_BY_HOP or low in ("host", "content-length", "accept-encoding", "user-agent"):
                continue
            # Cloudflare compares browser fingerprint headers with the TLS
            # connection fingerprint; curl's TLS stack must not advertise Chrome.
            if low.startswith("sec-ch-ua") or low.startswith("sec-fetch-") or low in (
                "accept",
                "accept-language",
                "upgrade-insecure-requests",
                "priority",
                "dnt",
                "cache-control",
                "pragma",
            ):
                continue
            if low in ("origin", "referer") and "127.0.0.1" in value:
                value = re.sub(r"https?://127\.0\.0\.1(:\d+)?", UPSTREAM, value)
                value = re.sub(r"https?://localhost(:\d+)?", UPSTREAM, value)
            cmd += ["--header", "%s: %s" % (key, value)]
        cmd += ["--header", "Host: " + UPSTREAM_HOST]
        cmd += ["--header", "User-Agent: " + BROWSER_UA]
        cmd += ["--header", "Accept: */*"]
        cmd += ["--header", "Accept-Language: zh-CN"]
        if self.command not in ("GET", "HEAD", "OPTIONS"):
            cmd += ["--header", "Origin: " + UPSTREAM]
            cmd += ["--header", "Referer: " + UPSTREAM + "/"]

        if body is not None:
            body_in = os.path.join(work, "body.in")
            with open(body_in, "wb") as fh:
                fh.write(body)
            cmd += ["--data-binary", "@" + body_in]

        cmd.append(UPSTREAM + path)

        if os.environ.get("GW_DEBUG"):
            sys.stderr.write("REQ %s %s\n" % (self.command, path))

        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=150)
            if not os.path.exists(hdr_path) or not os.path.exists(body_path):
                self.send_error(502, "upstream failed: " + proc.stderr.decode("utf-8", "replace")[:300])
                return
            with open(hdr_path, "r", encoding="iso-8859-1", errors="replace") as fh:
                blocks = parse_header_blocks(fh.read())
            with open(body_path, "rb") as fh:
                payload = fh.read()
        finally:
            shutil.rmtree(work, ignore_errors=True)

        status, reason, headers = blocks[-1] if blocks else (502, "Bad Gateway", [])

        self.send_response_only(status, reason)
        for key, value in headers:
            low = key.lower()
            if low in HOP_BY_HOP or low in DROP_RESPONSE_HEADERS:
                continue
            if low == "set-cookie":
                value = rewrite_set_cookie(value)
            elif low == "location":
                value = value.replace(UPSTREAM, self._local_origin())
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload and self.command != "HEAD":
            self.wfile.write(payload)
            self.wfile.flush()

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_PATCH = _forward
    do_DELETE = _forward
    do_HEAD = _forward
    do_OPTIONS = _forward

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    global UPSTREAM, UPSTREAM_HOST

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--upstream", default=os.environ.get("GW_UPSTREAM", UPSTREAM))
    parser.add_argument("--upstream-proxy", default="127.0.0.1:7897")
    args = parser.parse_args()

    UPSTREAM = args.upstream.rstrip("/")
    UPSTREAM_HOST = urllib.parse.urlsplit(UPSTREAM).hostname or "example.com"

    STATE["proxy"] = args.upstream_proxy
    STATE["listen"] = ("127.0.0.1", args.port)

    httpd = Server(STATE["listen"], Handler)
    print(
        "gateway: http://127.0.0.1:%d  ->  %s via %s"
        % (args.port, UPSTREAM, args.upstream_proxy),
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
