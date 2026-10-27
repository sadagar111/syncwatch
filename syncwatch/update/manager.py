"""Everything about updates in one place, for the UI. No Qt here.

The UI calls these from a background thread:
    check(manual)       -> CheckResult  (is there something newer, and is it genuine?)
    prepare(found, ...) -> downloads, verifies and stages it for the next start
and on the main thread:
    startup_news()      -> what boot.py did since last time (updated, went back, failed)
    mark_healthy()      -> "this version started fine" (stops an automatic rollback)
    request_rollback()  -> go back to the previous version at the next start
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from .. import __version__
from . import github, install, keys as keys_module, manifest as manifest_module, source as source_module
from .layout import Layout
from .manifest import Manifest, ManifestError
from .version import Version

log = logging.getLogger("syncwatch.update")

BOOT_VERSION_REQUIRED_DEFAULT = 1


@dataclass
class CheckResult:
    status: str                       # "update" | "current" | "none_published" | "skipped" | "error" | "off"
    manifest: Optional[Manifest] = None
    tag: str = ""
    code: str = ""                    # SW code when status == "error" or "off"
    technical: str = ""
    download_bytes: int = 0
    needs_runtime: bool = False


@dataclass
class StartupNews:
    updated_to: str = ""
    updated_from: str = ""
    rolled_back_from: str = ""
    rolled_back_to: str = ""
    apply_error: str = ""
    notes: str = ""
    problems: List[str] = field(default_factory=list)


class UpdateManager:
    def __init__(self, layout: Optional[Layout] = None, repo: Optional[str] = None,
                 trusted_keys: Optional[Iterable[str]] = None, current: str = __version__,
                 client: Optional[github.GitHub] = None):
        self.layout = layout or Layout()
        self.repo = source_module.GITHUB_REPO if repo is None else repo
        self.keys = list(keys_module.TRUSTED_KEYS if trusted_keys is None else trusted_keys)
        self.current = Version(current)
        if client is None and self.repo:
            # For tests only: point at a local stand-in for GitHub. Signatures are still required,
            # and only https or this PC's own address (127.0.0.1) is accepted, so this can't be abused.
            server = os.environ.get("SYNCWATCH_UPDATE_SERVER", "")
            client = (github.GitHub(self.repo, api_base=server, web_base=server) if server
                      else github.GitHub(self.repo))
        self.client = client
        self.lock = threading.Lock()

    # ------------------------------------------------------------ availability
    def availability(self) -> CheckResult:
        """Why updates can't run here (status "off"), or status "current" if they can."""
        if not self.layout.installed:
            return CheckResult("off", code="SW-709", technical="running from source code")
        if not self.repo or not self.keys:
            return CheckResult("off", code="SW-711", technical="no update repository or signing key configured")
        return CheckResult("current")

    # ------------------------------------------------------------ checking
    def check(self, include_test: bool, skip_version: str = "", manual: bool = False) -> CheckResult:
        if not self.repo or not self.keys:
            return CheckResult("off", code="SW-711", technical="no update repository or signing key configured")
        try:
            release = self.client.newest(include_test, manifest_module.MAX_MANIFEST_BYTES)
        except github.NetError as exc:
            return CheckResult("error", code=exc.code, technical=exc.technical)
        if release is None:
            return CheckResult("none_published")
        try:
            manifest_module.verify_signature(release.manifest, release.signature, self.keys)
            found = manifest_module.parse(release.manifest, self.repo)
        except ManifestError as exc:
            log.error("refused update manifest: %s", exc)
            return CheckResult("error", code="SW-703", technical=str(exc))
        if release.tag not in ("latest", f"v{found.version}"):
            return CheckResult("error", code="SW-703",
                               technical=f"manifest says {found.version} but the release is {release.tag}")
        if found.version <= self.current:
            if found.version < self.current and manual:
                log.info("newest published %s is older than this version %s", found.version, self.current)
            return CheckResult("current", manifest=found)
        if found.version.is_test and not include_test:
            return CheckResult("current", manifest=found)
        if skip_version and str(found.version) == skip_version and not manual:
            return CheckResult("skipped", manifest=found, tag=f"v{found.version}")
        needs_runtime = found.runtime_id != self.layout.current_runtime()
        if needs_runtime and found.runtime is None:
            return CheckResult("error", code="SW-703", technical="new runtime needed but not in the manifest")
        size = found.app_zip.size + (found.runtime.size if needs_runtime and found.runtime else 0)
        return CheckResult("update", manifest=found, tag=f"v{found.version}", download_bytes=size,
                           needs_runtime=needs_runtime)

    # ------------------------------------------------------------ downloading and staging
    def prepare(self, result: CheckResult, progress: Optional[github.Progress] = None,
                cancel: Optional[threading.Event] = None) -> None:
        """Download, verify and stage. Raises github.NetError / ChecksumError / Cancelled / install.InstallError."""
        found = result.manifest
        if found is None or result.status != "update":
            raise ValueError("nothing to prepare")
        if not self.lock.acquire(blocking=False):
            raise install.InstallError("SW-706", "another update is already being prepared")
        try:
            downloads = self.layout.downloads
            downloads.mkdir(parents=True, exist_ok=True)
            needed = result.download_bytes * 3  # zip + unpacked files + margin
            install.check_space(self.layout.root, needed)
            files = [(found.app_zip, "app")]
            if result.needs_runtime and found.runtime is not None:
                files.insert(0, (found.runtime, "runtime"))
            total = sum(ref.size for ref, _ in files)
            done_before = 0
            for ref, kind in files:
                tag = ref.tag or result.tag

                def report(done, size, speed, eta, offset=done_before):
                    if progress:
                        left = (total - offset - done) / speed if speed > 0 else eta
                        progress(offset + done, total, speed, left)

                path = self.client.download(self.client.asset_url(tag, ref.name), downloads / ref.name,
                                            ref.size, ref.sha256, report, cancel)
                if kind == "runtime":
                    install.stage_runtime(self.layout, path, found.runtime_id)
                else:
                    install.stage_app(self.layout, path, str(found.version))
                done_before += ref.size
            self.layout.update_state(staged_version=str(found.version), staged_notes=found.notes[:5000])
            self._clean_downloads(keep={ref.name for ref, _ in files})
        finally:
            self.lock.release()

    def _clean_downloads(self, keep) -> None:
        for path in self.layout.downloads.glob("*"):
            if path.name not in keep and path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass

    def staged(self) -> str:
        return self.layout.staged_version()

    # ------------------------------------------------------------ after (re)start
    def startup_news(self) -> StartupNews:
        state = self.layout.read_state()
        news = StartupNews()
        if state.get("just_updated"):
            news.updated_to = str(state["just_updated"].get("version", ""))
            news.updated_from = str(state["just_updated"].get("from", ""))
            news.notes = str(state.get("staged_notes", ""))
        if state.get("rolled_back"):
            news.rolled_back_from = str(state["rolled_back"].get("version", ""))
            news.rolled_back_to = str(state["rolled_back"].get("to", ""))
            news.problems.append("SW-707")
            self.layout.update_state(rolled_back=None)
        if state.get("apply_error"):
            news.apply_error = str(state["apply_error"])
            news.problems.append("SW-706")
            self.layout.update_state(apply_error=None)
        return news

    def mark_healthy(self) -> None:
        state = self.layout.read_state()
        if state.get("just_updated") or state.get("start_attempts"):
            self.layout.update_state(just_updated=None, start_attempts=None, staged_notes=None,
                                     healthy_version=str(self.current), healthy_at=time.time())

    def request_rollback(self) -> bool:
        if not self.layout.has_previous():
            return False
        self.layout.update_state(rollback_requested=True)
        return True
