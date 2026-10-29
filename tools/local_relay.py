"""A relay server on this PC that behaves like the real one (relay/src/index.js).

Used by the automatic tests, and handy for trying rooms without internet:

    python tools/local_relay.py            (then SyncWatch with SYNCWATCH_RELAY=http://127.0.0.1:8787)

What a relay does, here and on Cloudflare:

* ``GET  /v1/health``        -> {"ok": true, ...}
* ``GET  /v1/ch/<32 hex>``    (WebSocket) joins a channel. Every binary message a
  member sends goes to all *other* members of that channel. Nothing is stored.
* ``POST /v1/ch/<32 hex>``    delivers one message (the request body) to everyone
  currently on that channel. Used for friends' mailboxes.

Limits (the same numbers as relay/src/index.js, checked by tests/test_relay.py):
messages up to 64 KiB, binary only, 32 connections per channel, 20 messages
per second per connection (bursts of 60; extra ones are dropped), 5 posts per
second per channel (bursts of 20).
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import ssl
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syncwatch.net.websocket import (ConnectionClosed, ProtocolError, WebSocket,  # noqa: E402
                                     accept_key, parse_http_head)

MAX_MESSAGE = 64 * 1024
MAX_CONNECTIONS = 32
RATE_PER_S, BURST = 20.0, 60.0
POST_RATE_PER_S, POST_BURST = 5.0, 20.0
CHANNEL = re.compile(r"^/v1/ch/([0-9a-f]{32})$")
REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
           411: "Length Required", 413: "Payload Too Large", 426: "Upgrade Required", 429: "Too Many Requests",
           503: "Service Unavailable"}


class Bucket:
    """Token bucket: ``rate`` per second on average, at most ``burst`` at once."""

    def __init__(self, rate: float, burst: float):
        self.rate, self.burst = rate, burst
        self.tokens, self.at = burst, time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


def _lingering_close(sock: socket.socket, seconds: float = 2.0, limit: int = 4 * MAX_MESSAGE) -> None:
    """After an answer: stop sending, and read (and drop) whatever the client still sends until it closes.

    Closing a socket with unread data (say, the body of a POST refused with 429 before it was
    read) makes the system reset the connection, and Windows then throws away the answer the
    client hasn't read yet: the client sees "connection reset" instead of 429."""
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        return
    end = time.monotonic() + seconds
    received = 0
    while received < limit:
        left = end - time.monotonic()
        if left <= 0:
            return
        try:
            sock.settimeout(left)
            chunk = sock.recv(65536)
        except OSError:   # timeout, reset: nothing more to wait for
            return
        if not chunk:
            return
        received += len(chunk)


class LocalRelay:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, certfile: Optional[str] = None,
                 keyfile: Optional[str] = None, quiet: bool = True):
        self.host = host
        self._requested_port = port
        self.quiet = quiet
        self._tls: Optional[ssl.SSLContext] = None
        if certfile:
            self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._tls.load_cert_chain(certfile, keyfile)
        self._server: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._channels: Dict[str, Set[WebSocket]] = {}
        self._post_buckets: Dict[str, Bucket] = {}
        self._open: Set[socket.socket] = set()
        self.port = 0
        self.stopped = threading.Event()
        self.stats = {"connections": 0, "messages": 0, "dropped": 0, "posts": 0}

    # ------------------------------------------------------------------ control
    @property
    def url(self) -> str:
        return f"{'https' if self._tls else 'http'}://{self.host}:{self.port}"

    def start(self) -> "LocalRelay":
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self._requested_port))
        server.listen(128)
        self._server = server
        self.port = server.getsockname()[1]
        threading.Thread(target=self._accept_loop, name="local-relay", daemon=True).start()
        return self

    def stop(self) -> None:
        self.stopped.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        self.drop_all()

    def drop_all(self) -> None:
        """Cut every connection at once, like a relay restart or the internet going away."""
        with self._lock:
            sockets = list(self._open)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def count(self, channel: str) -> int:
        with self._lock:
            return len(self._channels.get(channel, ()))

    def _log(self, text: str) -> None:
        if not self.quiet:
            print(time.strftime("%H:%M:%S"), text, flush=True)

    # ------------------------------------------------------------------ connections
    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self.stopped.is_set():
            try:
                sock, address = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), name="local-relay-conn", daemon=True).start()

    def _serve(self, sock: socket.socket) -> None:
        with self._lock:
            self._open.add(sock)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self._tls is not None:
                sock.settimeout(10)
                sock = self._tls.wrap_socket(sock, server_side=True)
                with self._lock:
                    self._open.add(sock)
            sock.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk or len(data) > 16384:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            request_line, headers = parse_http_head(head)
            parts = request_line.split(" ")
            if len(parts) != 3:
                return self._respond(sock, 400, "bad request")
            method, path = parts[0], parts[1].split("?", 1)[0]
            if path == "/v1/health":
                return self._respond(sock, 200, json.dumps({"ok": True, "service": "syncwatch-relay",
                                                            "version": 1}), "application/json")
            match = CHANNEL.match(path)
            if not match:
                return self._respond(sock, 404, "not found")
            channel = match.group(1)
            if method == "POST":
                return self._post(sock, channel, headers, rest)
            if method != "GET":
                return self._respond(sock, 405, "method not allowed")
            if headers.get("upgrade", "").lower() != "websocket":
                return self._respond(sock, 426, "expected a WebSocket")
            key = headers.get("sec-websocket-key", "")
            if headers.get("sec-websocket-version") != "13" or len(key) != 24:
                return self._respond(sock, 400, "bad WebSocket request")
            with self._lock:
                members = self._channels.setdefault(channel, set())
                if len(members) >= MAX_CONNECTIONS:
                    full = True
                else:
                    full = False
            if full:
                return self._respond(sock, 503, "channel full")
            sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n").encode("ascii"))
            sock.settimeout(None)
            ws = WebSocket(sock, rest, max_message=MAX_MESSAGE, mask_outgoing=False, expect_masked=True)
            ws.linger = 1.0  # so a refused client still reads why (1009, 1003) instead of a reset
            self._member_loop(ws, channel)
        except (OSError, ProtocolError, ValueError):
            pass
        finally:
            with self._lock:
                self._open.discard(sock)
            try:
                sock.close()
            except OSError:
                pass

    def _respond(self, sock: socket.socket, status: int, body: str, kind: str = "text/plain") -> None:
        data = body.encode("utf-8")
        sock.sendall((f"HTTP/1.1 {status} {REASONS.get(status, '')}\r\nContent-Type: {kind}\r\n"
                      f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode("ascii") + data)
        _lingering_close(sock)

    def _post(self, sock: socket.socket, channel: str, headers: Dict[str, str], rest: bytes) -> None:
        try:
            length = int(headers.get("content-length", ""))
        except ValueError:
            return self._respond(sock, 411, "length required")
        if length > MAX_MESSAGE:
            return self._respond(sock, 413, "too big")
        if length <= 0:
            return self._respond(sock, 400, "empty")
        with self._lock:
            bucket = self._post_buckets.setdefault(channel, Bucket(POST_RATE_PER_S, POST_BURST))
            allowed = bucket.take()
        if not allowed:
            return self._respond(sock, 429, "slow down")
        body = rest
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                return
            body += chunk
        self.stats["posts"] += 1
        self._broadcast(channel, body[:length], None)
        self._respond(sock, 202, "")

    def _member_loop(self, ws: WebSocket, channel: str) -> None:
        bucket = Bucket(RATE_PER_S, BURST)
        with self._lock:
            self._channels.setdefault(channel, set()).add(ws)
            self.stats["connections"] += 1
        self._log(f"+ {channel[:8]} ({self.count(channel)})")
        try:
            while not self.stopped.is_set():
                message = ws.recv()
                if isinstance(message, str):
                    ws.close(1003, "binary only")
                    return
                if not bucket.take():
                    self.stats["dropped"] += 1
                    continue
                self.stats["messages"] += 1
                self._broadcast(channel, message, ws)
        except (ConnectionClosed, ProtocolError, OSError):
            pass
        finally:
            with self._lock:
                members = self._channels.get(channel)
                if members is not None:
                    members.discard(ws)
                    if not members:
                        del self._channels[channel]
            self._log(f"- {channel[:8]} ({self.count(channel)})")
            ws.close()

    def _broadcast(self, channel: str, message: bytes, sender: Optional[WebSocket]) -> None:
        with self._lock:
            targets: List[WebSocket] = [ws for ws in self._channels.get(channel, ()) if ws is not sender]
        for ws in targets:
            try:
                ws.send(message)
            except (ConnectionClosed, OSError):
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="A SyncWatch relay on this PC (for testing).")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    relay = LocalRelay(args.host, args.port, quiet=False).start()
    print(f"SyncWatch test relay running at {relay.url}  (Ctrl+C to stop)")
    print(f"Start SyncWatch with  SYNCWATCH_RELAY={relay.url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        relay.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
