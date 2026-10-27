"""A tiny stand-in for GitHub's release pages and API, for testing updates offline.

It serves exactly the URLs SyncWatch uses:
    /repos/<owner>/<repo>/releases                      API list (for test versions)
    /<owner>/<repo>/releases/latest/download/<file>      redirect to the newest normal release
    /<owner>/<repo>/releases/download/<tag>/<file>       the file (supports resuming with Range)
and can misbehave on purpose (break off a download once, rate-limit).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict
from urllib.parse import unquote, urlparse


class FakeGitHub:
    def __init__(self, repo: str):
        self.repo = repo
        self.releases: Dict[str, dict] = {}     # tag -> {"prerelease": bool, "assets": {name: bytes}, "order": n}
        self.drop_once_after = 0                 # break the next big download off after N bytes
        self.rate_limited = False
        self.requests = []
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, tag: str, assets: Dict[str, bytes], prerelease: bool = False) -> None:
        self.releases[tag] = {"prerelease": prerelease, "assets": dict(assets), "order": len(self.releases)}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _latest_stable(self):
        stable = [(r["order"], tag) for tag, r in self.releases.items() if not r["prerelease"]]
        return max(stable)[1] if stable else None

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, code, body=b"", headers=None):
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                path = unquote(urlparse(self.path).path)
                fake.requests.append(path)
                if fake.rate_limited:
                    self.send(403, b'{"message":"API rate limit exceeded"}', {"X-RateLimit-Remaining": "0"})
                    return
                if path == f"/repos/{fake.repo}/releases":
                    listing = []
                    for tag, release in sorted(fake.releases.items(), key=lambda kv: -kv[1]["order"]):
                        listing.append({"tag_name": tag, "draft": False, "prerelease": release["prerelease"],
                                        "assets": [{"name": n, "browser_download_url": f"{fake.base}/{fake.repo}/releases/download/{tag}/{n}"}
                                                   for n in release["assets"]]})
                    self.send(200, json.dumps(listing).encode(), {"Content-Type": "application/json"})
                    return
                prefix_latest = f"/{fake.repo}/releases/latest/download/"
                if path.startswith(prefix_latest):
                    tag = fake._latest_stable()
                    if tag is None:
                        self.send(404, b"Not Found")
                        return
                    self.send(302, b"", {"Location": f"{fake.base}/{fake.repo}/releases/download/{tag}/{path[len(prefix_latest):]}"})
                    return
                prefix = f"/{fake.repo}/releases/download/"
                if path.startswith(prefix):
                    tag, _, name = path[len(prefix):].partition("/")
                    data = fake.releases.get(tag, {}).get("assets", {}).get(name)
                    if data is None:
                        self.send(404, b"Not Found")
                        return
                    start = 0
                    range_header = self.headers.get("Range")
                    if range_header and range_header.startswith("bytes="):
                        start = int(range_header[6:].split("-")[0])
                    body = data[start:]
                    status = 206 if start else 200
                    if fake.drop_once_after and len(body) > fake.drop_once_after:
                        cut, fake.drop_once_after = fake.drop_once_after, 0
                        self.send_response(status)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body[:cut])
                        self.wfile.flush()
                        self.connection.shutdown(2)  # break off mid-download
                        return
                    self.send(status, body, {"Content-Type": "application/octet-stream"})
                    return
                self.send(404, b"Not Found")

        return Handler
