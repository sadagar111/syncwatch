"""A small WebSocket client (RFC 6455), so SyncWatch needs no extra library.

Only what the relay needs: one connection, binary messages, pings. It is
strict about what it accepts from the server, because that comes from the
internet: wrong handshake answers, masked or malformed frames, unknown
opcodes and oversized messages all end the connection with an error instead
of being guessed at.

Thread use: one thread calls ``recv()``; any thread may call ``send()``,
``ping()`` and ``close()``.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import time
from typing import Dict, Optional, Tuple, Union
from urllib.parse import urlsplit

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONTINUATION, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
_DATA_OPS = (OP_TEXT, OP_BINARY)
MAX_HEADER_BYTES = 16 * 1024
MAX_ERROR_BODY = 2048
FRAME_FINISH_S = 30.0  # once a frame has started arriving, how long the rest may take

Message = Union[bytes, str]


class WebSocketError(OSError):
    """Anything that went wrong with the connection."""


class HandshakeError(WebSocketError):
    """The server didn't accept the WebSocket connection (status and a bit of its answer)."""

    def __init__(self, status: int, reason: str, body: str = ""):
        super().__init__(f"server answered {status} {reason}".strip() + (f": {body}" if body else ""))
        self.status = status
        self.body = body


class ProtocolError(WebSocketError):
    """The server sent something that isn't valid WebSocket."""


class ConnectionClosed(WebSocketError):
    def __init__(self, code: int = 1006, reason: str = ""):
        super().__init__(f"connection closed ({code}{': ' + reason if reason else ''})")
        self.code = code
        self.reason = reason


def _mask(data: bytes, key: bytes) -> bytes:
    """XOR with the 4-byte key repeated (as one big integer: much faster than byte by byte)."""
    if not data:
        return data
    repeated = (key * (len(data) // 4 + 1))[:len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(data), "big")


def _frame(opcode: int, payload: bytes, mask: bool) -> bytes:
    length = len(payload)
    first = 0x80 | opcode  # FIN set: we never split our own messages
    mask_bit = 0x80 if mask else 0
    if length < 126:
        header = struct.pack("!BB", first, mask_bit | length)
    elif length < 1 << 16:
        header = struct.pack("!BBH", first, mask_bit | 126, length)
    else:
        header = struct.pack("!BBQ", first, mask_bit | 127, length)
    if mask:
        key = os.urandom(4)
        return header + key + _mask(payload, key)
    return header + payload


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + GUID).digest()).decode("ascii")


def _read_http_head(sock: socket.socket) -> Tuple[bytes, bytes]:
    """Read up to the end of the HTTP header block. Returns (head, bytes read after it)."""
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HEADER_BYTES:
            raise ProtocolError("the server's answer headers are too long")
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionClosed(1006, "closed during the handshake")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def parse_http_head(head: bytes) -> Tuple[str, Dict[str, str]]:
    """First line and headers (names lower-case) of an HTTP request or response."""
    try:
        lines = head.decode("iso-8859-1").split("\r\n")
    except UnicodeDecodeError:  # pragma: no cover (iso-8859-1 decodes anything)
        raise ProtocolError("unreadable headers") from None
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep:
            raise ProtocolError("malformed header line")
        headers[name.strip().lower()] = value.strip()
    return lines[0], headers


class WebSocket:
    def __init__(self, sock: socket.socket, buffered: bytes = b"", max_message: int = 1 << 20,
                 mask_outgoing: bool = True, expect_masked: bool = False):
        self.sock = sock
        self.max_message = max_message
        self._buf = bytearray(buffered)
        self._send_lock = threading.Lock()
        self._mask_outgoing = mask_outgoing    # clients mask, servers don't (tools/local_relay.py)
        self._expect_masked = expect_masked
        self._close_sent = False
        self.closed: Optional[ConnectionClosed] = None
        # After our close frame, keep reading (and discarding) this long before hanging up. Closing
        # with unread data makes Windows reset the connection, and the other side then never
        # sees our close frame (the relay sets this; clients don't need it).
        self.linger = 0.0
        self.last_received = time.monotonic()
        self.last_pong = 0.0

    # ------------------------------------------------------------------ connecting
    @classmethod
    def connect(cls, url: str, timeout: float = 10.0, user_agent: str = "SyncWatch",
                max_message: int = 1 << 20, ssl_context: Optional[ssl.SSLContext] = None) -> "WebSocket":
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss") or not parts.hostname:
            raise ValueError(f"not a WebSocket address: {url!r}")
        secure = parts.scheme == "wss"
        host = parts.hostname
        port = parts.port or (443 if secure else 80)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock = raw
            if secure:
                context = ssl_context or ssl.create_default_context()
                sock = context.wrap_socket(raw, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_header = host if parts.port is None else f"{host}:{port}"
            if ":" in host and not host.startswith("["):
                host_header = f"[{host}]" + ("" if parts.port is None else f":{port}")
            request = (f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\n"
                       f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                       f"User-Agent: {user_agent}\r\n\r\n")
            sock.sendall(request.encode("ascii"))
            head, rest = _read_http_head(sock)
            status_line, headers = parse_http_head(head)
            parts_line = status_line.split(" ", 2)
            if len(parts_line) < 2 or not parts_line[0].startswith("HTTP/1.") or not parts_line[1].isdigit():
                raise ProtocolError(f"not an HTTP answer: {status_line[:80]!r}")
            status = int(parts_line[1])
            if status != 101:
                body = rest[:MAX_ERROR_BODY]
                try:
                    length = min(int(headers.get("content-length", "0")), MAX_ERROR_BODY)
                    sock.settimeout(2.0)
                    while len(body) < length:
                        chunk = sock.recv(length - len(body))
                        if not chunk:
                            break
                        body += chunk
                except (ValueError, OSError):
                    pass
                raise HandshakeError(status, parts_line[2] if len(parts_line) > 2 else "",
                                     body.decode("utf-8", "replace").strip()[:200])
            if headers.get("upgrade", "").lower() != "websocket" or \
                    "upgrade" not in headers.get("connection", "").lower():
                raise ProtocolError("the server didn't switch to WebSocket")
            if headers.get("sec-websocket-accept") != accept_key(key):
                raise ProtocolError("the server's handshake answer doesn't match")
            if headers.get("sec-websocket-extensions"):
                raise ProtocolError("the server wants extensions we didn't ask for")
            sock.settimeout(None)
            return cls(sock, rest, max_message)
        except BaseException:
            try:
                raw.close()
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ sending
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        frame = _frame(opcode, payload, self._mask_outgoing)
        with self._send_lock:
            if self._close_sent and opcode != OP_CLOSE:
                raise ConnectionClosed(1000, "already closing")
            try:
                self.sock.sendall(frame)
            except OSError as exc:
                raise ConnectionClosed(1006, f"send failed: {exc}") from None

    def send(self, data: Message) -> None:
        if isinstance(data, str):
            self._send_frame(OP_TEXT, data.encode("utf-8"))
        else:
            self._send_frame(OP_BINARY, bytes(data))

    def ping(self, payload: bytes = b"") -> None:
        self._send_frame(OP_PING, payload[:125])

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Say goodbye (if we haven't yet) and drop the connection."""
        with self._send_lock:
            already = self._close_sent
            self._close_sent = True
        if not already:
            try:
                payload = struct.pack("!H", code) + reason.encode("utf-8")[:120]
                frame = _frame(OP_CLOSE, payload, self._mask_outgoing)
                with self._send_lock:
                    self.sock.sendall(frame)
            except OSError:
                pass
            if self.linger > 0:
                self._drain(self.linger)
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def _drain(self, seconds: float) -> None:
        """Stop sending, then read and discard until the other side hangs up (or time runs out)."""
        end = time.monotonic() + seconds
        try:
            self.sock.shutdown(socket.SHUT_WR)
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    return
                self.sock.settimeout(left)
                if not self.sock.recv(65536):
                    return
        except OSError:
            return

    # ------------------------------------------------------------------ receiving
    def _fill(self, count: int, deadline: Optional[float]) -> None:
        """Make sure ``count`` bytes are buffered. On timeout nothing is lost (they stay buffered)."""
        while len(self._buf) < count:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise socket.timeout("timed out")
                self.sock.settimeout(left)
            else:
                self.sock.settimeout(None)
            try:
                chunk = self.sock.recv(max(65536, count - len(self._buf)))
            except socket.timeout:
                raise
            except OSError as exc:
                raise ConnectionClosed(1006, f"connection lost: {exc}") from None
            if not chunk:
                raise ConnectionClosed(1006, "the connection dropped")
            self._buf += chunk

    def _next_frame(self, deadline: Optional[float]) -> Tuple[bool, int, bytes]:
        self._fill(2, deadline)
        # Once a frame has started, give the rest of it its own (generous) time.
        finish = time.monotonic() + FRAME_FINISH_S
        first, second = self._buf[0], self._buf[1]
        fin, opcode = bool(first & 0x80), first & 0x0F
        if first & 0x70:
            raise ProtocolError("reserved bits set (no extensions were agreed)")
        masked, length = bool(second & 0x80), second & 0x7F
        if masked != self._expect_masked:
            raise ProtocolError("masked frame from the server" if masked else "unmasked frame from a client")
        offset = 2
        if length == 126:
            self._fill(4, finish)
            length = struct.unpack("!H", self._buf[2:4])[0]
            offset = 4
        elif length == 127:
            self._fill(10, finish)
            length = struct.unpack("!Q", self._buf[2:10])[0]
            if length >> 63:
                raise ProtocolError("impossible frame length")
            offset = 10
        if opcode >= 0x8:
            if length > 125 or not fin:
                raise ProtocolError("malformed control frame")
        elif opcode not in (OP_CONTINUATION, OP_TEXT, OP_BINARY):
            raise ProtocolError(f"unknown frame type {opcode}")
        if length > self.max_message:
            raise ConnectionClosed(1009, "message too big")
        key = b""
        if masked:
            self._fill(offset + 4, finish)
            key = bytes(self._buf[offset:offset + 4])
            offset += 4
        self._fill(offset + length, finish)
        payload = bytes(self._buf[offset:offset + length])
        del self._buf[:offset + length]
        if masked:
            payload = _mask(payload, key)
        return fin, opcode, payload

    def recv(self, timeout: Optional[float] = None) -> Message:
        """The next message (bytes or str). Answers pings by itself.

        Raises ``socket.timeout`` if nothing arrives in time (call again later),
        ``ConnectionClosed`` when the connection ends, ``ProtocolError`` for
        invalid data (the connection is unusable after that).
        """
        if self.closed is not None:
            raise self.closed
        deadline = None if timeout is None else time.monotonic() + timeout
        parts: list = []
        message_op = None
        size = 0
        try:
            while True:
                if parts:  # in the middle of a message split into pieces: the rest must follow soon
                    try:
                        fin, opcode, payload = self._next_frame(time.monotonic() + FRAME_FINISH_S)
                    except socket.timeout:
                        raise ConnectionClosed(1006, "a message stopped halfway") from None
                else:
                    fin, opcode, payload = self._next_frame(deadline)
                self.last_received = time.monotonic()
                if opcode == OP_PING:
                    try:
                        self._send_frame(OP_PONG, payload)
                    except ConnectionClosed:
                        pass
                    continue
                if opcode == OP_PONG:
                    self.last_pong = time.monotonic()
                    continue
                if opcode == OP_CLOSE:
                    code, reason = 1005, ""
                    if len(payload) >= 2:
                        code = struct.unpack("!H", payload[:2])[0]
                        reason = payload[2:].decode("utf-8", "replace")
                    elif len(payload) == 1:
                        raise ProtocolError("malformed close frame")
                    self.close(code if code not in (1005, 1006, 1015) else 1000)
                    self.closed = ConnectionClosed(code, reason)
                    raise self.closed
                if opcode == OP_CONTINUATION:
                    if message_op is None:
                        raise ProtocolError("continuation without a start")
                elif message_op is not None:
                    raise ProtocolError("new message before the previous one finished")
                else:
                    message_op = opcode
                size += len(payload)
                if size > self.max_message:
                    raise ConnectionClosed(1009, "message too big")
                parts.append(payload)
                if fin:
                    data = b"".join(parts)
                    if message_op == OP_TEXT:
                        try:
                            return data.decode("utf-8")
                        except UnicodeDecodeError:
                            raise ProtocolError("text message isn't UTF-8") from None
                    return data
        except ConnectionClosed as exc:
            if self.closed is None:
                self.closed = exc
                if exc.code == 1009:
                    self.close(1009, "message too big")
            raise
        except ProtocolError:
            self.closed = ConnectionClosed(1002, "protocol error")
            self.close(1002, "protocol error")
            raise
