"""The direct connection between two SyncWatch apps (host and one friend).

This is the step-1 connection: the friend connects straight to the host's IP
address (same home network or Tailscale). Step 3 replaces it with rooms that
go through an encrypted relay, but the sync logic on top stays the same.

Safety and robustness compared to version 0.1:
* The host accepts one friend at a time. A second connection is politely
  refused instead of kicking out your friend (SW-310).
* Every received line is size-limited and checked (see protocol.py); too
  many messages per second or malformed data closes the connection (SW-309).
* A friend that silently disappears (sleep, Wi-Fi drop) is noticed within
  about 12 seconds instead of hanging forever (SW-307).
* On Windows the port is bound exclusively, so two SyncWatch windows can't
  both "host" on the same port and steal each other's connections.

All callbacks go through ``on_event(dict)``, which must be thread-safe (the
UI passes ``queue.Queue.put`` and handles events on its own thread).
Events the link generates itself have a type starting with "_".
"""
from __future__ import annotations

import errno
import ipaddress
import logging
import re
import socket
import statistics
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .. import PROTOCOL_VERSION, __version__
from . import protocol

log = logging.getLogger("syncwatch.net")

PING_INTERVAL_S = 2.0
DEAD_AFTER_S = 12.0
CONNECT_TIMEOUT_S = 8.0
MAX_MSGS_PER_S = 60
BURST = 120
REFUSE_DRAIN_S = 3.0      # how long a refused extra caller gets to read "full"
MAX_REFUSING = 8          # more callers than this at once are just cut off
_refusing = threading.BoundedSemaphore(MAX_REFUSING)

_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_UNREACHABLE_NETWORK = {errno.ENETUNREACH, errno.ENETDOWN, 10050, 10051}
_HOST_UNREACHABLE = {errno.EHOSTUNREACH, 10065}
_IN_USE = {errno.EADDRINUSE, 10048}
_ACCESS = {errno.EACCES, 10013}


def parse_address(text: str, default_port: int) -> Tuple[str, int]:
    """'100.1.2.3', '100.1.2.3:6000', 'pc-name', '[fd7a::1]:6000' -> (host, port).

    Raises ValueError for anything that isn't a plausible address (SW-303).
    """
    text = (text or "").strip()
    if not text or len(text) > 260 or any(ch.isspace() for ch in text):
        raise ValueError("empty or contains spaces")
    host, port = text, default_port
    if text.startswith("["):
        match = re.match(r"^\[([0-9A-Fa-f:.]+)\](?::(\d{1,5}))?$", text)
        if not match:
            raise ValueError("bad IPv6 address")
        host = match.group(1)
        port = int(match.group(2)) if match.group(2) else default_port
    elif text.count(":") == 1:
        host, port_text = text.split(":")
        if not port_text.isdigit():
            raise ValueError("port is not a number")
        port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("port out of range")
    try:
        ipaddress.ip_address(host)
        return host, port
    except ValueError:
        pass
    if not _HOSTNAME.match(host) or host.replace(".", "").isdigit():
        raise ValueError("not an IP address or computer name")
    return host, port


def local_addresses() -> List[Tuple[str, str]]:
    """This PC's IPv4 addresses with a label: [('100.101.1.2', 'Tailscale'), ...]."""
    found = set()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))  # sends nothing; just picks the outgoing interface
        found.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    result = []
    for text in found:
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local:
            continue
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            label = "Tailscale"
        elif ip.is_private:
            label = "home network"
        else:
            label = "internet"
        result.append((text, label))
    order = {"Tailscale": 0, "home network": 1, "internet": 2}
    return sorted(result, key=lambda item: (order[item[1]], item[0]))


def _kill(sock: Optional[socket.socket]) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _refuse(sock: socket.socket) -> None:
    """Tell an extra caller "full" and hang up politely.

    Simply closing would reset the connection (their hello is still unread), and
    Windows then throws away our "full" message before their app can read it:
    they'd see "Network problem" instead of "already watching with someone".
    So: send, signal the end of our data, read whatever they still send (at most
    REFUSE_DRAIN_S seconds and 64 KB), then close.
    """
    try:
        sock.settimeout(2)
        sock.sendall(protocol.encode({"t": "bye", "reason": "full"}))
        sock.shutdown(socket.SHUT_WR)
        end = time.monotonic() + REFUSE_DRAIN_S
        received = 0
        while time.monotonic() < end and received < 65536:
            sock.settimeout(max(0.05, end - time.monotonic()))
            chunk = sock.recv(4096)
            if not chunk:
                break  # they read our message and closed their side
            received += len(chunk)
    except OSError:
        pass
    finally:
        _kill(sock)
        _refusing.release()


class _Connection:
    """State for one open connection."""

    def __init__(self, sock: socket.socket, peer: str):
        self.sock = sock
        self.peer = peer
        self.greeted = False           # received a valid hello
        self.last_seen = time.monotonic()
        self.rtts: List[float] = []
        self.tokens = float(BURST)
        self.token_time = time.monotonic()
        self.closed = False
        self.reason = ""


class Link:
    def __init__(self, on_event: Callable[[Dict], None], hello: Callable[[], Dict]):
        self.on_event = on_event
        self.hello = hello              # builds our hello message when a connection opens
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.server: Optional[socket.socket] = None
        self.conn: Optional[_Connection] = None
        self.session = 0
        self.is_host = False
        self.connecting = False         # a join attempt is in progress
        self.port = 0

    # ------------------------------------------------------------ public
    @property
    def connected(self) -> bool:
        conn = self.conn
        return conn is not None and conn.greeted and not conn.closed

    @property
    def rtt(self) -> float:
        """Median round-trip time in seconds (0 if unknown)."""
        conn = self.conn
        if conn is None or not conn.rtts:
            return 0.0
        return statistics.median(conn.rtts)

    def host(self, port: int) -> bool:
        session = self.close()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", port))
            server.listen(4)
        except OSError as exc:
            server.close()
            code = "SW-301" if exc.errno in _IN_USE else "SW-302"
            self._problem(code, f"bind port {port}: {type(exc).__name__}: {exc}", port=port)
            return False
        server.settimeout(0.5)
        with self.lock:
            self.server = server
            self.is_host = True
            self.port = port
        threading.Thread(target=self._accept_loop, args=(server, session), daemon=True,
                         name="sw-accept").start()
        self._status(f"Hosting on port {port}. Waiting for your friend…")
        return True

    def join(self, address: str, default_port: int) -> bool:
        try:
            host, port = parse_address(address, default_port)
        except ValueError as exc:
            self._problem("SW-303", f"{address!r}: {exc}")
            return False
        session = self.close()
        with self.lock:
            self.is_host = False
            self.connecting = True
        threading.Thread(target=self._connect, args=(host, port, session), daemon=True,
                         name="sw-connect").start()
        return True

    def send(self, message: Dict) -> None:
        conn = self.conn
        if conn is not None and conn.greeted and not conn.closed:
            self._send_raw(conn, message)

    def close(self) -> int:
        """Stop hosting / disconnect. Returns the new session number."""
        with self.lock:
            self.session += 1
            server, conn = self.server, self.conn
            self.server, self.conn = None, None
            self.connecting = False
            session = self.session
        if conn is not None:
            conn.closed = True
            self._send_raw(conn, {"t": "bye", "reason": "closing"})
            _kill(conn.sock)
        if server is not None:
            _kill(server)
        return session

    # ------------------------------------------------------------ internals
    def _status(self, text: str) -> None:
        self.on_event({"t": "_status", "text": text})

    def _problem(self, code: str, technical: str = "", **params) -> None:
        self.on_event({"t": "_problem", "code": code, "technical": technical, "params": params})

    def _accept_loop(self, server: socket.socket, session: int) -> None:
        while self.session == session:
            try:
                sock, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            peer = addr[0]
            with self.lock:
                busy = self.conn is not None and not self.conn.closed
            if busy or self.session != session:
                log.info("refusing extra connection from %s (already connected)", peer)
                if _refusing.acquire(blocking=False):
                    threading.Thread(target=_refuse, args=(sock,), name="sw-refuse", daemon=True).start()
                else:
                    _kill(sock)  # someone is hammering the port: don't spend threads on it
                continue
            self._attach(sock, peer, session)

    def _connect(self, host: str, port: int, session: int) -> None:
        address = f"{host}:{port}"
        self._status(f"Connecting to {address}…")
        error: Optional[OSError] = None
        try:
            sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
            if self.session == session:
                self.connecting = False
        except socket.gaierror as exc:
            code, error = "SW-306", exc
        except (socket.timeout, TimeoutError) as exc:
            code, error = "SW-304", exc
        except ConnectionRefusedError as exc:
            code, error = "SW-305", exc
        except OSError as exc:
            code, error = ("SW-311" if exc.errno in _UNREACHABLE_NETWORK
                           else "SW-304" if exc.errno in _HOST_UNREACHABLE else "SW-312"), exc
        if error is None:
            if self.session != session:
                _kill(sock)
                return
            self._attach(sock, host, session)
            return
        if self.session == session:
            self.connecting = False
            self._status("Not connected")
            self._problem(code, f"connect {address}: {type(error).__name__}: {error}", address=address)

    def _attach(self, sock: socket.socket, peer: str, session: int) -> None:
        sock.settimeout(None)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        conn = _Connection(sock, peer)
        with self.lock:
            if session != self.session:
                _kill(sock)
                return
            self.conn = conn
        self._send_raw(conn, self.hello())
        threading.Thread(target=self._read_loop, args=(conn,), daemon=True, name="sw-read").start()
        threading.Thread(target=self._ping_loop, args=(conn,), daemon=True, name="sw-ping").start()

    def _send_raw(self, conn: _Connection, message: Dict) -> None:
        try:
            data = protocol.encode(message)
            with self.send_lock:
                conn.sock.sendall(data)
        except protocol.BadMessage as exc:
            log.error("refused to send oversized %s message: %s", message.get("t"), exc)
        except OSError:
            pass  # the read loop notices the broken connection

    def _drop(self, conn: _Connection, reason: str, code: str = "", technical: str = "", **params) -> None:
        """Close one connection and tell the app (only once)."""
        with self.lock:
            if conn.closed:
                return
            conn.closed = True
            conn.reason = reason
            mine = self.conn is conn
            if mine:
                self.conn = None
        _kill(conn.sock)
        if not mine:
            return
        if code:
            self._problem(code, technical, **params)
        if conn.greeted:
            self.on_event({"t": "_disconnected", "reason": reason})
        if self.is_host and self.server is not None:
            self._status(f"Hosting on port {self.port}. Waiting for your friend…")
        else:
            self._status("Not connected")

    def _allow(self, conn: _Connection) -> bool:
        """Token bucket: at most MAX_MSGS_PER_S messages per second on average."""
        now = time.monotonic()
        conn.tokens = min(BURST, conn.tokens + (now - conn.token_time) * MAX_MSGS_PER_S)
        conn.token_time = now
        if conn.tokens < 1:
            return False
        conn.tokens -= 1
        return True

    def _read_loop(self, conn: _Connection) -> None:
        reader = conn.sock.makefile("rb")
        try:
            while not conn.closed:
                line = reader.readline(protocol.MAX_LINE_BYTES + 1)
                if not line:
                    self._drop(conn, "closed by friend", "SW-307" if conn.greeted else "",
                               "connection closed by the other side")
                    return
                conn.last_seen = time.monotonic()
                if not self._allow(conn):
                    self._drop(conn, "flood", "SW-309", "too many messages per second")
                    return
                if not line.endswith(b"\n"):
                    self._drop(conn, "bad data", "SW-309", "message line too long")
                    return
                try:
                    message = protocol.decode(line.rstrip(b"\r\n"))
                except protocol.BadMessage as exc:
                    self._drop(conn, "bad data", "SW-309", f"invalid message: {exc}")
                    return
                if message is None:
                    continue  # a message type from a newer version: ignore it
                if not self._handle(conn, message):
                    return
        except OSError as exc:
            self._drop(conn, "connection error", "SW-307" if conn.greeted else "SW-312",
                       f"{type(exc).__name__}: {exc}")
        finally:
            try:
                reader.close()
            except OSError:
                pass

    def _handle(self, conn: _Connection, message: Dict) -> bool:
        kind = message["t"]
        if kind == "ping":
            self._send_raw(conn, {"t": "pong", "id": message["id"]})
            return True
        if kind == "pong":
            rtt = time.monotonic() - message["id"]
            if 0 <= rtt < 30:
                conn.rtts = (conn.rtts + [rtt])[-5:]
            return True
        if kind == "bye":
            reason = message["reason"]
            if reason == "full":
                self._drop(conn, "host busy", "SW-310", "host already has a friend connected")
            elif reason == "version":
                self._drop(conn, "version mismatch", "SW-308", "friend refused our version",
                           theirs=message.get("app", "?"), mine=__version__)
            else:
                self._drop(conn, "friend left", "", "")
            return False
        if kind == "hello":
            if message["v"] != PROTOCOL_VERSION:
                self._send_raw(conn, {"t": "bye", "reason": "version", "app": __version__,
                                      "v": PROTOCOL_VERSION})
                self._drop(conn, "version mismatch", "SW-308",
                           f"protocol {message['v']} vs {PROTOCOL_VERSION}",
                           theirs=message.get("app", "?"), mine=__version__)
                return False
            first = not conn.greeted
            conn.greeted = True
            if first:
                self.on_event({"t": "_connected", "peer": conn.peer, "host": self.is_host})
            self.on_event(message)
            return True
        if not conn.greeted:
            self._drop(conn, "bad data", "SW-309", f"{kind} before hello")
            return False
        self.on_event(message)
        return True

    def _ping_loop(self, conn: _Connection) -> None:
        while not conn.closed:
            if time.monotonic() - conn.last_seen > DEAD_AFTER_S:
                self._drop(conn, "timeout", "SW-307" if conn.greeted else "SW-304",
                           f"no data for {DEAD_AFTER_S:.0f} s", address=conn.peer)
                return
            self._send_raw(conn, {"t": "ping", "id": time.monotonic()})
            time.sleep(PING_INTERVAL_S)
