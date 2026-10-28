"""The relay connection: WebSocket client, relay behaviour, channels that reconnect.

The "RelayConformance" tests describe what a relay must do. They run against
tools/local_relay.py, and on GitHub also against the real relay code
(relay/src/index.js running in Cloudflare's local runtime, see
.github/workflows/relay.yml): set SYNCWATCH_TEST_RELAY=http://127.0.0.1:8787.
"""
import datetime
import http.client
import ipaddress
import json
import os
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syncwatch.net import relay as relay_module  # noqa: E402
from syncwatch.net.relay import Relay, RelayError  # noqa: E402
from syncwatch.net.websocket import (ConnectionClosed, HandshakeError, ProtocolError,  # noqa: E402
                                     WebSocket, accept_key, parse_http_head)
from tools.local_relay import BURST, MAX_CONNECTIONS, MAX_MESSAGE, LocalRelay  # noqa: E402

EXTERNAL = os.environ.get("SYNCWATCH_TEST_RELAY", "").rstrip("/")


def channel_id(n: int = 0) -> str:
    return os.urandom(15).hex() + f"{n:02x}"


class RelayConformance(unittest.TestCase):
    """What every SyncWatch relay must do (local one, or the real code via SYNCWATCH_TEST_RELAY)."""

    @classmethod
    def setUpClass(cls):
        if EXTERNAL:
            cls.local = None
            cls.base = EXTERNAL
        else:
            cls.local = LocalRelay().start()
            cls.base = cls.local.url
        cls.ws_base = cls.base.replace("https://", "wss://").replace("http://", "ws://")

    @classmethod
    def tearDownClass(cls):
        if cls.local is not None:
            cls.local.stop()

    def setUp(self):
        self.sockets = []

    def tearDown(self):
        for ws in self.sockets:
            ws.close()

    def connect(self, channel: str) -> WebSocket:
        ws = WebSocket.connect(f"{self.ws_base}/v1/ch/{channel}", timeout=10)
        self.sockets.append(ws)
        return ws

    def recv_all(self, ws: WebSocket, wait: float = 1.0):
        got = []
        end = time.monotonic() + wait
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return got
            try:
                got.append(ws.recv(timeout=left))
            except socket.timeout:
                return got

    def http(self, method: str, path: str, body: bytes = None, headers=None):
        request = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_health(self):
        status, body = self.http("GET", "/v1/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual((data["ok"], data["service"], data["version"]), (True, "syncwatch-relay", 1))

    def test_messages_reach_the_others_but_not_the_sender(self):
        channel = channel_id()
        a, b, c = self.connect(channel), self.connect(channel), self.connect(channel)
        time.sleep(0.2)
        a.send(b"\x01hello")
        self.assertEqual(b.recv(timeout=5), b"\x01hello")
        self.assertEqual(c.recv(timeout=5), b"\x01hello")
        self.assertEqual(self.recv_all(a, 0.5), [])

    def test_channels_are_separate(self):
        a, b = self.connect(channel_id(1)), self.connect(channel_id(2))
        time.sleep(0.2)
        a.send(b"private")
        self.assertEqual(self.recv_all(b, 0.7), [])

    def test_order_is_kept(self):
        channel = channel_id()
        a, b = self.connect(channel), self.connect(channel)
        time.sleep(0.2)
        for n in range(40):
            a.send(struct.pack("!I", n))
        got = [struct.unpack("!I", m)[0] for m in self.recv_all(b, 3.0)]
        self.assertEqual(got, list(range(40)))

    def test_biggest_message_passes_and_bigger_is_refused(self):
        channel = channel_id()
        a, b = self.connect(channel), self.connect(channel)
        time.sleep(0.2)
        big = os.urandom(MAX_MESSAGE)
        a.send(big)
        self.assertEqual(b.recv(timeout=10), big)
        a.send(os.urandom(MAX_MESSAGE + 1))
        with self.assertRaises(ConnectionClosed) as caught:
            a.recv(timeout=10)
        self.assertEqual(caught.exception.code, 1009)

    def test_text_is_refused(self):
        a = self.connect(channel_id())
        a.send("hello")
        with self.assertRaises(ConnectionClosed) as caught:
            a.recv(timeout=10)
        self.assertEqual(caught.exception.code, 1003)

    def test_flooding_is_limited(self):
        channel = channel_id()
        a, b = self.connect(channel), self.connect(channel)
        time.sleep(0.2)
        started = time.monotonic()
        for n in range(150):
            a.send(b"x" * 10)
        spent = time.monotonic() - started
        got = len(self.recv_all(b, 2.0))
        allowed = BURST + 20 * (spent + 0.5)
        self.assertGreaterEqual(got, BURST - 5)
        self.assertLessEqual(got, allowed + 5)
        self.assertLess(got, 150)

    def test_channel_size_limit(self):
        channel = channel_id()
        for _ in range(MAX_CONNECTIONS):
            self.connect(channel)
        with self.assertRaises(HandshakeError) as caught:
            self.connect(channel)
        self.assertEqual(caught.exception.status, 503)

    def test_post_delivers_to_everyone_on_the_channel(self):
        channel = channel_id()
        a, b = self.connect(channel), self.connect(channel)
        time.sleep(0.2)
        status, _ = self.http("POST", f"/v1/ch/{channel}", b"mail", {"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 202)
        self.assertEqual(a.recv(timeout=5), b"mail")
        self.assertEqual(b.recv(timeout=5), b"mail")

    def test_post_limits(self):
        channel = channel_id()
        self.assertEqual(self.http("POST", f"/v1/ch/{channel}", b"")[0], 400)
        self.assertEqual(self.http("POST", f"/v1/ch/{channel}", b"x" * (MAX_MESSAGE + 1))[0], 413)
        statuses = [self.http("POST", f"/v1/ch/{channel}", b"x")[0] for _ in range(30)]
        self.assertIn(429, statuses)
        self.assertEqual(statuses[0], 202)

    def test_wrong_addresses(self):
        self.assertEqual(self.http("GET", "/")[0], 404)
        self.assertEqual(self.http("GET", "/v1/ch/not-a-channel")[0], 404)
        self.assertEqual(self.http("GET", f"/v1/ch/{channel_id()}")[0], 426)   # not a WebSocket
        self.assertEqual(self.http("PUT", f"/v1/ch/{channel_id()}", b"x")[0], 405)
        with self.assertRaises(HandshakeError) as caught:
            self.connect("ABC")
        self.assertEqual(caught.exception.status, 404)

    def test_ping_is_answered(self):
        a = self.connect(channel_id())
        a.ping(b"hi")
        self.recv_all(a, 1.0)
        self.assertGreater(a.last_pong, 0)


# --------------------------------------------------------------------------------------------- scripted server
class Scripted:
    """A fake server that does the handshake (or not) and then sends exactly the given bytes."""

    def __init__(self, response=None, frames=b"", delay_after=0.0, more=b""):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.response, self.frames, self.delay_after, self.more = response, frames, delay_after, more
        self.received = b""
        self.done = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        conn, _ = self.server.accept()
        self.server.close()
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        head, _, _ = data.partition(b"\r\n\r\n")
        _, headers = parse_http_head(head)
        response = self.response or ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                     f"Connection: Upgrade\r\nSec-WebSocket-Accept: "
                                     f"{accept_key(headers['sec-websocket-key'])}\r\n\r\n").encode()
        conn.sendall(response + self.frames)
        if self.delay_after:
            time.sleep(self.delay_after)
            conn.sendall(self.more)
        conn.settimeout(2)
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                self.received += chunk
        except OSError:
            pass
        self.done.set()
        conn.close()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/v1/ch/{'0' * 32}"


def frame(opcode, payload=b"", fin=True, rsv=0, masked=False, length=None):
    first = (0x80 if fin else 0) | (rsv << 4) | opcode
    n = len(payload) if length is None else length
    header = bytes([first]) + (bytes([(0x80 if masked else 0) | n]) if n < 126
                               else bytes([(0x80 if masked else 0) | 126]) + struct.pack("!H", n))
    if masked:
        return header + b"\x00\x00\x00\x00" + payload
    return header + payload


class ClientStrictnessTests(unittest.TestCase):
    def test_refused_handshake_shows_status_and_reason(self):
        server = Scripted(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 12\r\n\r\nchannel full")
        with self.assertRaises(HandshakeError) as caught:
            WebSocket.connect(server.url, timeout=5)
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("channel full", str(caught.exception))

    def test_bad_handshake_answers(self):
        for response in (b"HTTP/1.1 101 OK\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: wrong\r\n\r\n",
                         b"SSH-2.0-OpenSSH\r\n\r\n"):
            with self.subTest(response=response[:20]), self.assertRaises(ProtocolError):
                WebSocket.connect(Scripted(response).url, timeout=5)

    def expect_protocol_error(self, frames):
        ws = WebSocket.connect(Scripted(frames=frames).url, timeout=5)
        with self.assertRaises((ProtocolError, ConnectionClosed)):
            ws.recv(timeout=5)
        ws.close()

    def test_invalid_frames_end_the_connection(self):
        self.expect_protocol_error(frame(2, b"x", masked=True))           # servers must not mask
        self.expect_protocol_error(frame(2, b"x", rsv=4))                 # no extensions agreed
        self.expect_protocol_error(frame(3, b"x"))                        # unknown opcode
        self.expect_protocol_error(frame(9, b"x" * 126))                  # control frame too long
        self.expect_protocol_error(frame(9, b"x", fin=False))             # split control frame
        self.expect_protocol_error(frame(0, b"x"))                        # continuation without start
        self.expect_protocol_error(frame(1, b"\xff\xfe"))                 # text that isn't UTF-8
        self.expect_protocol_error(frame(2, b"a", fin=False) + frame(2, b"b"))  # new message mid-message

    def test_split_message_with_ping_in_between(self):
        server = Scripted(frames=frame(2, b"ab", fin=False) + frame(0, b"cd", fin=False)
                          + frame(9, b"pp") + frame(0, b"ef"))
        ws = WebSocket.connect(server.url, timeout=5)
        self.assertEqual(ws.recv(timeout=5), b"abcdef")
        ws.close()
        server.done.wait(5)
        self.assertIn(b"\x8a", server.received)   # a pong went back (masked, as clients must)

    def test_too_big_and_close_codes(self):
        ws = WebSocket.connect(Scripted(frames=frame(2, b"x" * 300)).url, timeout=5)
        ws.max_message = 200
        with self.assertRaises(ConnectionClosed) as caught:
            ws.recv(timeout=5)
        self.assertEqual(caught.exception.code, 1009)
        ws = WebSocket.connect(Scripted(frames=frame(8, struct.pack("!H", 4000) + b"bye")).url, timeout=5)
        with self.assertRaises(ConnectionClosed) as caught:
            ws.recv(timeout=5)
        self.assertEqual((caught.exception.code, caught.exception.reason), (4000, "bye"))
        with self.assertRaises(ConnectionClosed):
            ws.recv(timeout=1)                    # stays closed

    def test_slow_frame_is_not_lost(self):
        whole = frame(2, b"slow message")
        server = Scripted(frames=whole[:1], delay_after=0.6, more=whole[1:])
        ws = WebSocket.connect(server.url, timeout=5)
        with self.assertRaises(socket.timeout):
            ws.recv(timeout=0.2)
        self.assertEqual(ws.recv(timeout=5), b"slow message")
        ws.close()


# --------------------------------------------------------------------------------------------- channels
class Collector:
    def __init__(self):
        self.messages = []
        self.states = []
        self.lock = threading.Lock()

    def on_message(self, data):
        with self.lock:
            self.messages.append(data)

    def on_state(self, state, error):
        with self.lock:
            self.states.append((state, error.code if error else None))

    def wait(self, predicate, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if predicate(self):
                    return True
            time.sleep(0.02)
        return False


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.old_backoff = relay_module.BACKOFF_S
        relay_module.BACKOFF_S = (0.1, 0.2, 0.4)
        self.local = LocalRelay().start()
        self.relay = Relay(self.local.url)
        self.channels = []

    def tearDown(self):
        for channel in self.channels:
            channel.close()
        self.local.stop()
        relay_module.BACKOFF_S = self.old_backoff

    def open(self, cid):
        collector = Collector()
        channel = self.relay.channel(cid, collector.on_message, collector.on_state).start()
        self.channels.append(channel)
        return channel, collector

    def test_send_receive_and_reconnect(self):
        cid = channel_id()
        a, ca = self.open(cid)
        b, cb = self.open(cid)
        self.assertTrue(ca.wait(lambda c: ("connected", None) in c.states))
        self.assertTrue(cb.wait(lambda c: ("connected", None) in c.states))
        self.assertTrue(a.send(b"one"))
        self.assertTrue(cb.wait(lambda c: c.messages == [b"one"]))
        self.local.drop_all()
        self.assertTrue(ca.wait(lambda c: any(s[0] == "waiting" for s in c.states)))
        self.assertTrue(ca.wait(lambda c: c.states[-1] == ("connected", None)))
        self.assertTrue(cb.wait(lambda c: c.states[-1] == ("connected", None)))
        self.assertGreaterEqual(a.connects, 2)
        self.assertTrue(a.send(b"two"))
        self.assertTrue(cb.wait(lambda c: c.messages == [b"one", b"two"]))
        a.close()
        self.assertTrue(ca.wait(lambda c: c.states[-1] == ("stopped", None)))
        self.assertFalse(a.send(b"three"))

    def test_relay_not_reachable(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()                       # nothing listens here now
        relay = Relay(f"http://127.0.0.1:{port}")
        collector = Collector()
        channel = relay.channel(channel_id(), collector.on_message, collector.on_state).start()
        self.channels.append(channel)
        self.assertTrue(collector.wait(lambda c: ("waiting", "SW-402") in c.states))

    def test_full_channel(self):
        cid = channel_id()
        raw = [WebSocket.connect(f"{self.relay.ws_base}/v1/ch/{cid}") for _ in range(MAX_CONNECTIONS)]
        try:
            _, collector = self.open(cid)
            self.assertTrue(collector.wait(lambda c: ("waiting", "SW-404") in c.states))
        finally:
            for ws in raw:
                ws.close()

    def test_post_and_health(self):
        cid = channel_id()
        _, collector = self.open(cid)
        self.assertTrue(collector.wait(lambda c: ("connected", None) in c.states))
        self.relay.post(cid, b"letter")
        self.assertTrue(collector.wait(lambda c: c.messages == [b"letter"]))
        self.assertTrue(self.relay.post_later(cid, b"later"))
        self.assertTrue(collector.wait(lambda c: c.messages == [b"letter", b"later"]))
        self.assertEqual(self.relay.health()["service"], "syncwatch-relay")
        with self.assertRaises(ValueError):
            self.relay.post("nope", b"x")

    def test_addresses(self):
        self.assertEqual(Relay("https://relay.example.org/").ws_base, "wss://relay.example.org")
        for bad in ("http://relay.example.org", "relay.example.org", "https://a b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Relay(bad)


def _self_signed(folder: Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SyncWatch test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = folder / "cert.pem", folder / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_file, key_file


class TlsTests(unittest.TestCase):
    def test_wss_with_certificate_checks(self):
        folder = Path(tempfile.mkdtemp(prefix="sw-tls-"))
        cert_file, key_file = _self_signed(folder)
        local = LocalRelay(certfile=str(cert_file), keyfile=str(key_file)).start()
        try:
            trusted = ssl.create_default_context(cafile=str(cert_file))
            relay = Relay(local.url.replace("127.0.0.1", "127.0.0.1"), ssl_context=trusted)
            # http(s)://127.0.0.1 is only allowed for tests on this PC; it's https here.
            self.assertTrue(relay.ws_base.startswith("wss://"))
            cid = channel_id()
            a = WebSocket.connect(relay.channel_url(cid), ssl_context=trusted)
            b = WebSocket.connect(relay.channel_url(cid), ssl_context=trusted)
            time.sleep(0.2)
            a.send(b"secret")
            self.assertEqual(b.recv(timeout=5), b"secret")
            a.close()
            b.close()
            self.assertEqual(relay.health()["ok"], True)
            # Without trusting that certificate the connection is refused (SW-403).
            untrusting = Relay(local.url)
            with self.assertRaises(RelayError) as caught:
                untrusting.health()
            self.assertEqual(caught.exception.code, "SW-403")
        finally:
            local.stop()


if __name__ == "__main__":
    unittest.main()
