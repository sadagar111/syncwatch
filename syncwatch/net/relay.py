"""Talking to the relay server: channels that reconnect by themselves, and posts.

The relay (relay/src/index.js, or tools/local_relay.py for tests) passes
messages between everyone on the same *channel* and stores nothing. A room is
one channel; each person's mailbox is another. Everything sent through it is
already encrypted (syncwatch/room/crypto.py), so the relay is not trusted
with anything.

``Channel`` keeps one WebSocket open in a background thread: it reconnects
after drops (waiting a little longer each time), pings now and then so dead
connections are noticed, and hands every received message to a callback.
Callbacks run in that background thread; the app forwards them to its main
thread.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional

from .. import __version__
from ..room.codes import CodeError, check_relay_address
from .websocket import ConnectionClosed, HandshakeError, ProtocolError, WebSocket

log = logging.getLogger("syncwatch.relay")

MAX_MESSAGE = 64 * 1024
BACKOFF_S = (1, 2, 4, 8, 15, 30)
PING_EVERY_S = 25.0
DEAD_AFTER_S = 70.0
STABLE_AFTER_S = 30.0          # a connection that lasted this long resets the waiting time
_CHANNEL_ID = re.compile(r"^[0-9a-f]{32}$")

# Channel states (passed to on_state)
CONNECTING, CONNECTED, WAITING, STOPPED = "connecting", "connected", "waiting", "stopped"


class RelayError(Exception):
    """A relay problem with an SW code for the popup and a technical description."""

    def __init__(self, code: str, technical: str, status: int = 0):
        super().__init__(f"{code}: {technical}")
        self.code = code
        self.technical = technical
        self.status = status


def classify(exc: BaseException) -> RelayError:
    """Turn a network exception into the problem the user sees."""
    if isinstance(exc, RelayError):
        return exc
    technical = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        exc = exc.reason  # urllib wraps the real cause (e.g. a certificate problem)
    if isinstance(exc, HandshakeError):
        if exc.status == 503:
            return RelayError("SW-404", technical, exc.status)
        return RelayError("SW-405", technical, exc.status)
    if isinstance(exc, ssl.SSLCertVerificationError) or (isinstance(exc, ssl.SSLError)
                                                         and "CERTIFICATE" in str(exc).upper()):
        return RelayError("SW-403", technical)
    if isinstance(exc, (ProtocolError, ssl.SSLError)):
        return RelayError("SW-405", technical)
    return RelayError("SW-402", technical)


class Relay:
    """One relay server, by its web address (https://..., or http://127.0.0.1:... for tests)."""

    def __init__(self, address: str, ssl_context: Optional[ssl.SSLContext] = None):
        try:
            self.address = check_relay_address(address)
        except CodeError as exc:
            raise ValueError(str(exc)) from None
        self.ws_base = "wss://" + self.address[len("https://"):] if self.address.startswith("https://") \
            else "ws://" + self.address[len("http://"):]
        self.ssl_context = ssl_context
        self.user_agent = f"SyncWatch/{__version__}"
        self._posts: "queue.Queue" = queue.Queue(maxsize=200)
        self._poster: Optional[threading.Thread] = None

    def channel_url(self, channel_id: str) -> str:
        if not _CHANNEL_ID.match(channel_id):
            raise ValueError("channel ids are 32 lower-case hex characters")
        return f"{self.ws_base}/v1/ch/{channel_id}"

    def _open(self, request: urllib.request.Request, timeout: float):
        if self.ssl_context is not None:
            return urllib.request.urlopen(request, timeout=timeout, context=self.ssl_context)
        return urllib.request.urlopen(request, timeout=timeout)

    def health(self, timeout: float = 8.0) -> Dict:
        request = urllib.request.Request(f"{self.address}/v1/health", headers={"User-Agent": self.user_agent})
        try:
            with self._open(request, timeout) as response:
                data = json.loads(response.read(4096).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RelayError("SW-405", f"health check answered {exc.code}", exc.code) from None
        except (OSError, ValueError) as exc:
            raise classify(exc) from None
        if not isinstance(data, dict) or data.get("service") != "syncwatch-relay":
            raise RelayError("SW-405", "that address isn't a SyncWatch relay")
        return data

    def post(self, channel_id: str, data: bytes, timeout: float = 10.0) -> None:
        """Deliver one message to everyone on a channel right now (raises RelayError)."""
        if not 0 < len(data) <= MAX_MESSAGE:
            raise ValueError("message size out of range")
        url = f"{self.address}/v1/ch/{channel_id}"
        if not _CHANNEL_ID.match(channel_id):
            raise ValueError("bad channel id")
        request = urllib.request.Request(url, data=bytes(data), method="POST",
                                         headers={"Content-Type": "application/octet-stream",
                                                  "User-Agent": self.user_agent})
        try:
            with self._open(request, timeout) as response:
                response.read(1024)
        except urllib.error.HTTPError as exc:
            code = "SW-406" if exc.code == 429 else "SW-405"
            raise RelayError(code, f"post answered {exc.code}", exc.code) from None
        except OSError as exc:
            raise classify(exc) from None

    def post_later(self, channel_id: str, data: bytes,
                   on_error: Optional[Callable[[RelayError], None]] = None) -> bool:
        """Post in the background (never blocks the caller). False if too much is queued."""
        try:
            self._posts.put_nowait((channel_id, data, on_error))
        except queue.Full:
            return False
        if self._poster is None or not self._poster.is_alive():
            self._poster = threading.Thread(target=self._post_loop, name="sw-relay-post", daemon=True)
            self._poster.start()
        return True

    def _post_loop(self) -> None:
        while True:
            try:
                channel_id, data, on_error = self._posts.get(timeout=30)
            except queue.Empty:
                return
            try:
                self.post(channel_id, data)
            except (RelayError, ValueError) as exc:
                log.info("post to %s… failed: %s", channel_id[:8], exc)
                if on_error is not None and isinstance(exc, RelayError):
                    on_error(exc)

    def channel(self, channel_id: str, on_message: Callable[[bytes], None],
                on_state: Callable[[str, Optional[RelayError]], None], name: str = "channel") -> "Channel":
        return Channel(self, channel_id, on_message, on_state, name)


class Channel:
    def __init__(self, relay: Relay, channel_id: str, on_message: Callable[[bytes], None],
                 on_state: Callable[[str, Optional[RelayError]], None], name: str = "channel"):
        self.relay = relay
        self.channel_id = channel_id
        self.url = relay.channel_url(channel_id)
        self.name = name
        self.on_message = on_message
        self.on_state = on_state
        self._ws: Optional[WebSocket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.state = STOPPED
        self.last_error: Optional[RelayError] = None
        self.connects = 0

    @property
    def connected(self) -> bool:
        return self.state == CONNECTED

    def start(self) -> "Channel":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"sw-relay-{self.name}", daemon=True)
            self._thread.start()
        return self

    def send(self, data: bytes) -> bool:
        """Send now if connected. Returns False if not (the caller decides whether that matters)."""
        ws = self._ws
        if ws is None or self.state != CONNECTED:
            return False
        if len(data) > MAX_MESSAGE:
            raise ValueError("message larger than the relay allows")
        try:
            ws.send(data)
            return True
        except (ConnectionClosed, OSError):
            return False

    def close(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            ws.close(1000, "bye")

    def reconnect_now(self) -> None:
        """Drop the current connection; the channel connects again at once (used after waking up)."""
        ws = self._ws
        if ws is not None:
            ws.close(1000, "reconnect")

    def _set_state(self, state: str, error: Optional[RelayError] = None) -> None:
        self.state = state
        self.last_error = error
        try:
            self.on_state(state, error)
        except Exception:  # never let a UI callback kill the network thread
            log.exception("channel state callback failed")

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            self._set_state(CONNECTING)
            started = time.monotonic()
            error: Optional[RelayError] = None
            try:
                ws = WebSocket.connect(self.url, timeout=10.0, user_agent=self.relay.user_agent,
                                       max_message=MAX_MESSAGE, ssl_context=self.relay.ssl_context)
            except Exception as exc:  # anything at all: report it and try again later
                error = classify(exc)
            else:
                self._ws = ws
                self.connects += 1
                self._set_state(CONNECTED)
                error = self._read_loop(ws)
                self._ws = None
                ws.close()
            if self._stop.is_set():
                break
            if time.monotonic() - started > STABLE_AFTER_S:
                attempt = 0
            wait = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            if error is not None and error.code == "SW-404":
                wait = max(wait, 15)  # channel full: don't hammer it
            attempt += 1
            log.info("%s: %s; trying again in %s s", self.name, error.technical if error else "disconnected", wait)
            self._set_state(WAITING, error)
            self._stop.wait(wait)
        self._set_state(STOPPED)

    def _read_loop(self, ws: WebSocket) -> Optional[RelayError]:
        last_ping = time.monotonic()
        while not self._stop.is_set():
            try:
                message = ws.recv(timeout=1.0)
            except socket.timeout:
                now = time.monotonic()
                if now - ws.last_received > DEAD_AFTER_S:
                    return RelayError("SW-402", "no answer from the relay for a while")
                if now - last_ping > PING_EVERY_S:
                    last_ping = now
                    try:
                        ws.ping(b"sw")
                    except (ConnectionClosed, OSError):
                        return RelayError("SW-402", "connection lost")
                continue
            except ConnectionClosed as exc:
                if self._stop.is_set():
                    return None
                if exc.code == 1009:
                    return RelayError("SW-405", "the relay refused a message as too large")
                return RelayError("SW-402", f"connection closed ({exc.code} {exc.reason})".strip())
            except (ProtocolError, OSError) as exc:
                return classify(exc)
            if isinstance(message, bytes):
                try:
                    self.on_message(message)
                except Exception:
                    log.exception("channel message callback failed")
        return None
