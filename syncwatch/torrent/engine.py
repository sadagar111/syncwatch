"""Streaming a torrent: libtorrent in a process of its own, plus a little web server for mpv.

    python -E -P engine.py --folder <downloads> [--listen 0.0.0.0:0] [--upload-kbps 0]
                           [--connections 200] [--dht yes] [--start-mb 8] [--metadata-timeout 120]

Talks in JSON lines (ASCII): commands come in on stdin, events go out on stdout.

Commands                                         Events
  {"cmd": "add", "source": magnet or path}         {"ev": "added", "hash": ...}
  {"cmd": "select", "index": n}                    {"ev": "files", "name", "hash", "files": [{index, path, size}]}
  {"cmd": "peer", "address": "1.2.3.4:5678"}       {"ev": "ready", "url": ..., "index", "size", "name"}
  {"cmd": "quit", "delete": true/false}            {"ev": "status", "state", "progress", "down", "up",
                                                          "peers", "seeds", "buffered", "have", "size"}
                                                   {"ev": "done", "path": ...}   the chosen file is complete
                                                   {"ev": "error", "kind", "message"}

How streaming works: only the chosen file downloads, in order. mpv reads it from
http://127.0.0.1:<port>/<secret token>/<index>/<name>. Every read waits until its pieces are there,
and the pieces just ahead of what mpv reads get deadlines, so libtorrent fetches those first (from
the fastest peers). A jump is a new read further on: the deadlines move there and everything after
it gets the higher priority. mpv reads the end of the file once (most films keep their index there),
so those pieces are fetched early too.

The bytes come from libtorrent itself (read_piece), not from the file on disk: a piece counts as
"there" once its checksum is right, which can be a moment before it's written to the file.

The web server only listens on 127.0.0.1 and only answers addresses with the secret token, so
neither other PCs nor web pages in a browser can read from it.
"""
from __future__ import annotations

import argparse
import collections
import http.server
import json
import math
import os
import queue
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import quote, unquote

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg",
                    ".ogv", ".flv")
CONTENT_TYPES = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
                 ".avi": "video/x-msvideo", ".mov": "video/quicktime", ".ts": "video/mp2t"}
WINDOW_BYTES = 40 * 1024 * 1024      # what gets deadlines ahead of what mpv reads
CHUNK = 256 * 1024
PIECE_WAIT_S = 90.0                  # a read waits this long for a piece, then the connection closes
SPACE_MARGIN = 200 * 1024 * 1024
CACHE_BYTES = 64 * 1024 * 1024       # pieces kept in memory for mpv (the last ones read, and ahead)
READ_AHEAD_PIECES = 4


def say(message: Dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS


def best_video(files: List[Dict]) -> Optional[int]:
    """The biggest video file (a film; samples and extras are smaller)."""
    videos = [f for f in files if is_video(f["path"])]
    return max(videos, key=lambda f: f["size"])["index"] if videos else None


class Engine:
    def __init__(self, args):
        import libtorrent as lt  # noqa: PLC0415 - only in this process
        self.lt = lt
        self.folder = Path(args.folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.start_bytes = max(1, args.start_mb) * 1024 * 1024
        self.metadata_timeout = args.metadata_timeout
        self.token = secrets.token_urlsafe(24)
        public = not args.listen.startswith("127.")
        self.session = lt.session({
            "listen_interfaces": args.listen,
            "enable_dht": args.dht, "enable_lsd": args.dht and public,
            "enable_upnp": public, "enable_natpmp": public,
            "upload_rate_limit": max(0, args.upload_kbps) * 1024,
            "connections_limit": max(10, args.connections),
            "user_agent": "SyncWatch",
            "alert_mask": (lt.alert.category_t.status_notification | lt.alert.category_t.error_notification
                           | lt.alert.category_t.storage_notification),
        })
        self.handle = None
        self.info = None
        self.index: Optional[int] = None
        self.file_size = 0
        self.file_offset = 0
        self.piece_length = 0
        self.added_at = 0.0
        self.ready_sent = False
        self.done_sent = False
        self.playhead = 0                 # the byte (in the file) mpv read from last
        self.changed = threading.Condition()
        self.pieces: "collections.OrderedDict[int, bytes]" = collections.OrderedDict()   # piece -> its bytes
        self.cached = 0
        self.asked: Set[int] = set()      # read_piece requested, answer not here yet
        self.commands: "queue.Queue[Dict]" = queue.Queue()
        self.running = True
        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, name="torrent-http", daemon=True).start()

    # ------------------------------------------------------------ commands
    def add(self, source: str) -> None:
        lt = self.lt
        if self.handle is not None:
            say({"ev": "error", "kind": "bad_source", "message": "one torrent at a time"})
            return
        try:
            if source.lower().startswith("magnet:?"):
                params = lt.parse_magnet_uri(source)
            else:
                path = Path(source)
                if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                    raise ValueError("not a .torrent file")
                params = lt.add_torrent_params()
                params.ti = lt.torrent_info(str(path))
            params.save_path = str(self.folder)
            params.flags |= lt.torrent_flags.sequential_download
            params.flags &= ~lt.torrent_flags.auto_managed
            params.flags &= ~lt.torrent_flags.paused
            self.handle = self.session.add_torrent(params)
        except Exception as exc:  # noqa: BLE001 - a broken link or file
            say({"ev": "error", "kind": "bad_source", "message": f"{type(exc).__name__}: {exc}"})
            return
        self.added_at = time.monotonic()
        say({"ev": "added", "hash": str(self.handle.info_hashes().get_best())})
        if self.handle.status().has_metadata:
            self._metadata()

    def select(self, index: int) -> None:
        if self.info is None or not 0 <= index < self.info.num_files():
            say({"ev": "error", "kind": "no_video", "message": f"no file {index}"})
            return
        files = self.info.layout()
        self.index = index
        self.file_size = files.file_size(index)
        self.file_offset = files.file_offset(index)
        self.piece_length = self.info.piece_length()
        free = shutil.disk_usage(self.folder).free
        if free < self.file_size + SPACE_MARGIN:
            say({"ev": "error", "kind": "disk", "message": f"{free} bytes free, {self.file_size} needed"})
            return
        priorities = [0] * self.info.num_files()
        priorities[index] = 4
        self.handle.prioritize_files(priorities)
        self._prioritize(0, ends=True)

    def connect(self, address: str) -> None:
        host, _, port = address.rpartition(":")
        try:
            if self.handle is not None and host and port.isdigit():
                self.handle.connect_peer((host.strip("[]"), int(port)))
        except Exception as exc:  # noqa: BLE001
            say({"ev": "error", "kind": "peer", "message": f"{type(exc).__name__}: {exc}"})

    def quit(self, delete: bool) -> None:
        self.running = False
        if self.handle is not None:
            options = self.lt.session.delete_files if delete else 0
            self.session.remove_torrent(self.handle, options)
            self.handle = None
            if delete:   # wait until libtorrent has let go of the files and deleted them
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    alerts = self.session.pop_alerts()
                    if any(type(a).__name__ in ("torrent_deleted_alert", "torrent_delete_failed_alert")
                           for a in alerts):
                        break
                    self.session.wait_for_alert(100)
        self.http.shutdown()

    # ------------------------------------------------------------ pieces
    def piece_of(self, position: int) -> int:
        return (self.file_offset + max(0, min(position, self.file_size - 1))) // self.piece_length

    def _prioritize(self, position: int, ends: bool = False) -> None:
        """Deadlines for the pieces just ahead of ``position``; after it: high priority, before: low."""
        handle = self.handle
        if handle is None or self.index is None:
            return
        first, last = self.piece_of(0), self.piece_of(self.file_size - 1)
        here = self.piece_of(position)
        window = max(4, math.ceil(WINDOW_BYTES / self.piece_length))
        priorities = list(handle.get_piece_priorities())
        for piece in range(first, last + 1):
            priorities[piece] = 7 if piece >= here else 1
        handle.prioritize_pieces(priorities)
        handle.clear_piece_deadlines()
        for step, piece in enumerate(range(here, min(last, here + window - 1) + 1)):
            if not handle.have_piece(piece):
                handle.set_piece_deadline(piece, 200 + step * 150)
        if ends:   # mpv looks at the end of the file once (the index of most films is there)
            for piece in (last - 1, last):
                if piece >= first and not handle.have_piece(piece):
                    handle.set_piece_deadline(piece, 300)

    def reading_from(self, position: int) -> None:
        """mpv started reading at ``position`` (a new connection: the start, a jump, the index)."""
        with self.changed:
            jumped = abs(position - self.playhead) > 4 * self.piece_length
            self.playhead = position
        near_end = position > self.file_size - 4 * self.piece_length
        if jumped and not near_end:
            self._prioritize(position)

    def wait_for(self, piece: int, timeout: float = PIECE_WAIT_S) -> bool:
        deadline = time.monotonic() + timeout
        with self.changed:
            while self.running and self.handle is not None:
                if self.handle.have_piece(piece):
                    return True
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.changed.wait(min(0.25, left))
        return False

    def piece_data(self, piece: int, timeout: float = 30.0) -> Optional[bytes]:
        """A downloaded piece's bytes, from libtorrent (asked once, then kept a while)."""
        deadline = time.monotonic() + timeout
        with self.changed:
            while self.running and self.handle is not None:
                data = self.pieces.get(piece)
                if data is not None:
                    self.pieces.move_to_end(piece)
                    return data
                if piece not in self.asked:
                    self.asked.add(piece)
                    self.handle.read_piece(piece)
                left = deadline - time.monotonic()
                if left <= 0:
                    self.asked.discard(piece)
                    return None
                self.changed.wait(min(0.25, left))
        return None

    def read_ahead(self, piece: int) -> None:
        """Ask for the next pieces mpv will read (if they're here), so they're ready in memory."""
        if self.handle is None:
            return
        last = self.piece_of(self.file_size - 1)
        with self.changed:
            for nxt in range(piece + 1, min(last, piece + READ_AHEAD_PIECES) + 1):
                if nxt not in self.pieces and nxt not in self.asked and self.handle.have_piece(nxt):
                    self.asked.add(nxt)
                    self.handle.read_piece(nxt)

    def _piece_read(self, alert) -> None:
        with self.changed:
            self.asked.discard(alert.piece)
            if alert.error.value() == 0 and alert.piece not in self.pieces:
                data = bytes(alert.buffer)
                self.pieces[alert.piece] = data
                self.cached += len(data)
                while self.cached > CACHE_BYTES and len(self.pieces) > 1:
                    _old, dropped = self.pieces.popitem(last=False)
                    self.cached -= len(dropped)
            self.changed.notify_all()

    def contiguous(self, position: int, limit: int = 400) -> int:
        """How many bytes from ``position`` on are already here (the buffer ahead of the playhead)."""
        if self.handle is None or self.index is None:
            return 0
        here, last = self.piece_of(position), self.piece_of(self.file_size - 1)
        count = 0
        for piece in range(here, min(last, here + limit) + 1):
            if not self.handle.have_piece(piece):
                break
            count += 1
        if count == 0:
            return 0
        end = min(self.file_size, (here + count) * self.piece_length - self.file_offset)
        return max(0, end - position)

    def file_path(self) -> Path:
        return self.folder / self.info.layout().file_path(self.index)

    # ------------------------------------------------------------ the main loop
    def _metadata(self) -> None:
        if self.info is not None:
            return
        self.info = self.handle.torrent_file()
        files = self.info.layout()
        pad = files.flag_pad_file
        listing = [{"index": i, "path": files.file_path(i), "size": files.file_size(i)}
                   for i in range(self.info.num_files()) if not files.file_flags(i) & pad]   # pad files aren't real
        say({"ev": "files", "name": self.info.name(), "hash": str(self.handle.info_hashes().get_best()),
             "files": listing})
        if best_video(listing) is None:
            say({"ev": "error", "kind": "no_video", "message": "no video file in this torrent"})

    def _check_ready(self) -> None:
        if self.ready_sent or self.index is None:
            return
        last = self.piece_of(self.file_size - 1)
        start_end = min(self.file_size - 1, self.start_bytes)
        needed = set(range(self.piece_of(0), self.piece_of(start_end) + 1)) | {last}
        if all(self.handle.have_piece(piece) for piece in needed):
            self.ready_sent = True
            name = os.path.basename(self.info.layout().file_path(self.index))
            port = self.http.server_address[1]
            url = f"http://127.0.0.1:{port}/{self.token}/{self.index}/{quote(name)}"
            say({"ev": "ready", "url": url, "index": self.index, "size": self.file_size, "name": name})

    def _status(self) -> None:
        if self.handle is None:
            return
        status = self.handle.status()
        message = {"ev": "status", "state": str(status.state).split(".")[-1], "down": int(status.download_rate),
                   "up": int(status.upload_rate), "peers": int(status.num_peers), "seeds": int(status.num_seeds)}
        if self.index is not None:
            have = self.handle.file_progress(flags=self.lt.torrent_handle.piece_granularity)[self.index]
            message.update(have=int(have), size=self.file_size, progress=round(have / max(1, self.file_size), 4),
                           buffered=self.contiguous(self.playhead))
            if have >= self.file_size and not self.done_sent:
                self.done_sent = True
                say({"ev": "done", "path": str(self.file_path())})
        say(message)

    def run(self) -> None:
        threading.Thread(target=self._read_commands, name="torrent-commands", daemon=True).start()
        say({"ev": "hello", "version": self.lt.__version__})
        next_status = 0.0
        while self.running:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                command = None
            if command is not None:
                self._do(command)
                continue
            for alert in self.session.pop_alerts():
                kind = type(alert).__name__
                if kind == "read_piece_alert":
                    self._piece_read(alert)
                elif kind == "metadata_received_alert":
                    self._metadata()
                elif kind in ("file_error_alert", "torrent_error_alert"):
                    say({"ev": "error", "kind": "disk" if "space" in alert.message().lower() else "other",
                         "message": alert.message()[:500]})
            with self.changed:
                self.changed.notify_all()       # pieces may have arrived: readers check again
            if self.handle is not None:
                if self.info is None and time.monotonic() - self.added_at > self.metadata_timeout:
                    say({"ev": "error", "kind": "no_peers", "message": "nobody shared the torrent's description"})
                    self.added_at = float("inf")
                self._check_ready()
            now = time.monotonic()
            if now >= next_status:
                next_status = now + 1.0
                self._status()
            self.session.wait_for_alert(100)

    def _read_commands(self) -> None:
        for line in sys.stdin:
            try:
                command = json.loads(line)
            except ValueError:
                continue
            if isinstance(command, dict):
                self.commands.put(command)
        self.commands.put({"cmd": "quit", "delete": False})   # SyncWatch went away: stop too

    def _do(self, command: Dict) -> None:
        name = command.get("cmd")
        if name == "add" and isinstance(command.get("source"), str):
            self.add(command["source"])
        elif name == "select" and isinstance(command.get("index"), int):
            self.select(command["index"])
        elif name == "peer" and isinstance(command.get("address"), str):
            self.connect(command["address"])
        elif name == "quit":
            self.quit(bool(command.get("delete")))

    # ------------------------------------------------------------ the web server for mpv
    def _handler_class(self):
        engine = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "SyncWatch"

            def log_message(self, *args):
                pass

            def do_HEAD(self):  # noqa: N802
                self._answer(body=False)

            def do_GET(self):  # noqa: N802
                self._answer(body=True)

            def _answer(self, body: bool) -> None:
                parts = [unquote(part) for part in self.path.split("?")[0].split("/") if part]
                if (len(parts) < 2 or not secrets.compare_digest(parts[0], engine.token)
                        or engine.index is None or parts[1] != str(engine.index)):
                    self.send_error(404)
                    return
                size = engine.file_size
                start, end = 0, size - 1
                ranged = self.headers.get("Range", "")
                if ranged.startswith("bytes="):
                    first, _, last = ranged[6:].split(",")[0].partition("-")
                    try:
                        if first:
                            start = int(first)
                            end = min(size - 1, int(last)) if last else size - 1
                        elif last:
                            start, end = max(0, size - int(last)), size - 1
                    except ValueError:
                        self.send_error(400)
                        return
                    if start >= size or start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                extension = os.path.splitext(parts[-1])[1].lower()
                self.send_header("Content-Type", CONTENT_TYPES.get(extension, "application/octet-stream"))
                self.end_headers()
                if not body:
                    return
                engine.reading_from(start)
                self._send(start, end)

            def _send(self, start: int, end: int) -> None:
                position = start
                try:
                    while position <= end and engine.running:
                        piece = engine.piece_of(position)
                        if not engine.wait_for(piece):
                            return   # took too long: mpv tries again (a new read)
                        data = engine.piece_data(piece)
                        if data is None:
                            return
                        engine.read_ahead(piece)
                        piece_start = piece * engine.piece_length - engine.file_offset   # in the file
                        stop = min(end + 1, piece_start + len(data))
                        while position < stop:
                            size = min(CHUNK, stop - position)
                            self.wfile.write(data[position - piece_start:position - piece_start + size])
                            position += size
                        with engine.changed:
                            if engine.playhead <= position:
                                engine.playhead = position
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return   # mpv closed this read (a jump): fine

        return Handler


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="engine.py", description="SyncWatch's torrent streaming helper")
    parser.add_argument("--folder", required=True)
    parser.add_argument("--listen", default="0.0.0.0:0,[::]:0")
    parser.add_argument("--upload-kbps", type=int, default=0)
    parser.add_argument("--connections", type=int, default=200)
    parser.add_argument("--dht", choices=("yes", "no"), default="yes")
    parser.add_argument("--start-mb", type=int, default=8)
    parser.add_argument("--metadata-timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    args.dht = args.dht == "yes"
    try:
        engine = Engine(args)
    except ImportError as exc:
        say({"ev": "error", "kind": "missing", "message": f"libtorrent isn't installed ({exc})"})
        return 1
    engine.run()
    return 0


def command(*arguments: str) -> list:
    """How SyncWatch starts this file (same flags as the converter: see library/convert.py)."""
    flags = ["-E"] + (["-P"] if sys.version_info >= (3, 11) else [])
    return [sys.executable, *flags, str(Path(__file__).resolve()), *map(str, arguments)]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
