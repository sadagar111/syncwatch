"""A web server on this PC for the web link checks (0.5.1): the files in a folder by name, with ranges
("Range: bytes=N-", as film hosts like Real-Debrid allow), and any *.html address as a web page."""
import http.server
import threading
from pathlib import Path


def start(folder: Path) -> str:
    """Serves ``folder`` until the program ends; returns its address (http://127.0.0.1:<port>)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_HEAD(self):  # noqa: N802
            self._answer(head=True)

        def do_GET(self):  # noqa: N802
            self._answer(head=False)

        def _send(self, status, kind, length, extra=()):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(length))
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()

        def _answer(self, head):
            name = self.path.split("?")[0].rsplit("/", 1)[-1]
            if name.endswith(".html"):
                body = b"<html><body>a page about a film</body></html>"
                self._send(200, "text/html; charset=utf-8", len(body))
                return None if head else self.wfile.write(body)
            path = folder / name
            if not name or not path.is_file():
                return self._send(404, "text/plain", 0)
            data = path.read_bytes()
            start, end, wanted = 0, len(data) - 1, self.headers.get("Range", "")
            extra = [("Accept-Ranges", "bytes")]
            if wanted.startswith("bytes="):
                first, _, last = wanted[6:].partition("-")
                start, end = int(first or 0), min(end, int(last)) if last else end
                extra.append(("Content-Range", f"bytes {start}-{end}/{len(data)}"))
            self._send(206 if wanted else 200, "video/x-matroska", end - start + 1, extra)
            if not head:
                try:
                    self.wfile.write(data[start:end + 1])
                except (BrokenPipeError, ConnectionResetError):
                    pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="film-server").start()
    return f"http://127.0.0.1:{server.server_address[1]}"
