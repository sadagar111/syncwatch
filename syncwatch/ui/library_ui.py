"""The film library in the window: your films, "Add film", and the steps behind it.

The list shows the films in the library folder (Settings > Playback > Library folder).
It's on the start screen, and in the Library window (📂 button, Ctrl+O) once a film is
open. Double-click a film to play it.

Add film, step by step (one film at a time, each step can be cancelled):
1. Pick a film anywhere on the PC (the window starts in Downloads).
2. Already in the library? Then it's simply opened, not copied again.
3. Check it: the converter reads which picture and sound tracks it has and whether they
   decode, then the player in use plays it invisibly for a moment.
4. If it plays: it's copied into the library (the original stays where it was), with a
   progress bar and the time left.
   If only the sound doesn't play: SyncWatch offers to convert the sound (the picture is
   copied untouched) and puts the converted copy into the library instead.
   If the player can't play the file at all, although the converter could read its
   picture: often the file's packaging is the problem, so SyncWatch offers a fresh copy
   made the same way, and keeps it only if that one plays (otherwise SW-223).
   If not even the converter can read a picture: it's damaged or no film (SW-223).
5. The film opens.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QFile, QFileSystemWatcher, QObject, QProcess, QStandardPaths, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMenu,
                               QMessageBox, QProgressBar, QPushButton, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import errors, paths
from ..filmstate import film_key
from ..library import films as library
from ..sync.controller import fmt_time
from ..units import Eta, human_size, human_time
from .players import PlayabilityTest

log = logging.getLogger("syncwatch.library")

CONVERTER = Path(__file__).resolve().parent.parent / "library" / "convert.py"
FILM_PATTERNS = " ".join(f"*{ext}" for ext in library.VIDEO_EXTENSIONS)
PROBE_LIMIT_MS = 120_000        # reading a film's tracks never takes this long; something hangs
REMOVE_AFTER_CLOSE_MS = 800     # removing the film that was just open: wait until it's let go of
SOUND_CODECS = {"ac3": "AC3 (Dolby Digital)", "eac3": "E-AC3 (Dolby Digital Plus)", "dts": "DTS",
                "truehd": "Dolby TrueHD", "mlp": "MLP"}


# =============================================================== the list of films
class LibraryPanel(QWidget):
    playRequested = Signal(object)        # Path
    addRequested = Signal()
    openFileRequested = Signal()
    showRequested = Signal(object)        # Path (None: just the folder)
    convertRequested = Signal(object)     # Path
    removeRequested = Signal(object)      # Path

    def __init__(self, parent=None):
        super().__init__(parent)
        title = QLabel("Your films")
        title.setObjectName("PanelTitle")
        self.add_button = QPushButton("➕  Add film…")
        self.add_button.setObjectName("Accent")
        self.add_button.setToolTip("Pick a film from Downloads or anywhere on this PC. SyncWatch checks that it "
                                   "plays and copies it into your library (the original stays where it was).")
        self.add_button.clicked.connect(self.addRequested)
        top = QHBoxLayout()
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(self.add_button)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Film", "Size", "Watched"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemDoubleClicked.connect(lambda item, _column: self.playRequested.emit(self._path(item)))
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.empty = QLabel("No films yet. Click <b>Add film</b> and pick one from Downloads or anywhere on this "
                            "PC: SyncWatch checks that it plays and copies it here. The original stays where it was.")
        self.empty.setWordWrap(True)
        self.empty.setObjectName("Dim")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self.play_button = QPushButton("▶  Play")
        self.play_button.clicked.connect(lambda: self.playRequested.emit(self.selected()))
        self.more = QToolButton()
        self.more.setText("More ▾")
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QMenu(self.more)
        self.more_menu.aboutToShow.connect(self._fill_more_menu)
        self.more.setMenu(self.more_menu)
        self.folder_label = QLabel()
        self.folder_label.setObjectName("Hint")
        self.folder_label.setTextFormat(Qt.TextFormat.PlainText)
        bottom = QHBoxLayout()
        bottom.addWidget(self.play_button)
        bottom.addWidget(self.more)
        bottom.addStretch(1)
        bottom.addWidget(self.folder_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.empty, 1)
        layout.addLayout(bottom)
        self._selection_changed()

    def set_films(self, films: List[library.Film], folder: Path, watched: Callable[[library.Film], str]) -> None:
        rows = [(film.path, film.size, watched(film)) for film in films]
        if rows == getattr(self, "_rows", None) and self.folder_label.text().endswith(str(folder)):
            return   # nothing changed (e.g. a copy in progress touched the folder): keep scroll and selection
        self._rows = rows
        current = self.selected()
        self.tree.clear()
        for film in films:
            item = QTreeWidgetItem([film.name, human_size(film.size), watched(film)])
            item.setData(0, Qt.ItemDataRole.UserRole, str(film.path))
            item.setToolTip(0, str(film.path))
            self.tree.addTopLevelItem(item)
            if current is not None and film.path == current:
                item.setSelected(True)
        if self.tree.topLevelItemCount() and not self.tree.selectedItems():
            self.tree.topLevelItem(0).setSelected(True)
        self.tree.setVisible(bool(films))
        self.empty.setVisible(not films)
        self.play_button.setVisible(bool(films))
        self.folder_label.setText(f"Library folder: {folder}")
        self._selection_changed()

    def selected(self) -> Optional[Path]:
        items = self.tree.selectedItems()
        return self._path(items[0]) if items else None

    @staticmethod
    def _path(item: QTreeWidgetItem) -> Path:
        return Path(item.data(0, Qt.ItemDataRole.UserRole))

    def _selection_changed(self) -> None:
        self.play_button.setEnabled(self.selected() is not None)

    def _film_actions(self, menu: QMenu, path: Optional[Path]) -> None:
        for text, signal in (("Play", self.playRequested), ("Show in folder", self.showRequested),
                             ("Convert the sound…", self.convertRequested),
                             ("Remove from library…", self.removeRequested)):
            action = menu.addAction(text)
            action.setEnabled(path is not None)
            action.triggered.connect(lambda _=False, s=signal: s.emit(path))

    def _fill_more_menu(self) -> None:
        self.more_menu.clear()
        path = self.selected()
        self._film_actions(self.more_menu, path)
        self.more_menu.addSeparator()
        self.more_menu.addAction("Open the library folder").triggered.connect(lambda: self.showRequested.emit(None))
        self.more_menu.addAction("Play a file without adding it…").triggered.connect(self.openFileRequested)

    def _context_menu(self, position) -> None:
        item = self.tree.itemAt(position)
        if item is None:
            return
        item.setSelected(True)
        menu = QMenu(self)
        self._film_actions(menu, self._path(item))
        menu.exec(self.tree.viewport().mapToGlobal(position))


class LibraryDialog(QDialog):
    """The Library window (📂 / Ctrl+O): the same list, while a film is open."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Library")
        self.resize(620, 460)
        self.panel = LibraryPanel(self)
        layout = QVBoxLayout(self)
        layout.addWidget(self.panel)


# =============================================================== a step-by-step job window
class JobDialog(QDialog):
    """Shows what a job (checking, copying, converting) is doing, with progress and Cancel."""
    cancelClicked = Signal()

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        self.step = QLabel()
        self.step.setWordWrap(True)
        self.step.setTextFormat(Qt.TextFormat.PlainText)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.detail = QLabel()
        self.detail.setObjectName("Dim")
        self.hint = QLabel()
        self.hint.setObjectName("Hint")
        self.hint.setWordWrap(True)
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self.cancelClicked)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel)
        layout = QVBoxLayout(self)
        layout.addWidget(self.step)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail)
        layout.addWidget(self.hint)
        layout.addLayout(buttons)

    def set_step(self, text: str, busy: bool = True, hint: str = "") -> None:
        self.step.setText(text)
        self.bar.setRange(0, 0 if busy else 1000)
        self.detail.setText("")
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))

    def set_progress(self, fraction: float, detail: str) -> None:
        self.bar.setRange(0, 1000)
        self.bar.setValue(int(1000 * max(0.0, min(1.0, fraction))))
        self.detail.setText(detail)

    def reject(self) -> None:   # Esc or the window's X: same as Cancel
        self.cancelClicked.emit()


# =============================================================== background workers
class CopyWorker(QObject):
    """Copies a film into the library in a background thread."""
    progress = Signal(object, object)    # done, total (bytes; objects so files over 2 GB fit)
    finished = Signal(object)            # None when done, else the exception

    def __init__(self, source: Path, target: Path, parent=None):
        super().__init__(parent)
        self.source, self.target = source, target
        self._cancel = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="library-copy", daemon=True).start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        try:
            library.copy_film(self.source, self.target, lambda done, total: self.progress.emit(done, total),
                              self._cancel.is_set)
            self.finished.emit(None)
        except BaseException as exc:  # noqa: BLE001 - every outcome goes back to the window
            self.finished.emit(exc)


class ConverterProcess(QObject):
    """Runs library/convert.py with SyncWatch's own Python, as a separate process."""
    message = Signal(dict)
    finished = Signal(int, str)      # exit code (-1: didn't run / killed), end of its error output

    def __init__(self, arguments: List[str], parent=None):
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        self.process.setArguments(["-I", str(CONVERTER), *arguments])
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self._buffer = b""
        self._done = False

    def start(self) -> None:
        log.info("converter: %s", " ".join(self.process.arguments()[2:]))
        self.process.start()

    def kill(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(3000)

    def _read(self) -> None:
        self._buffer += bytes(self.process.readAllStandardOutput())
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(message, dict):
                self.message.emit(message)

    def _finished(self, code: int, _status) -> None:
        self._read()
        if not self._done:
            self._done = True
            error_output = bytes(self.process.readAllStandardError()).decode("utf-8", "replace")
            self.finished.emit(code if _status == QProcess.ExitStatus.NormalExit else -1, error_output[-2000:])

    def _error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart and not self._done:
            self._done = True
            self.finished.emit(-1, f"couldn't start {sys.executable}: {self.process.errorString()}")


# =============================================================== Add film / Convert the sound
class AddFilmJob(QObject):
    """One "Add film" (or "Convert the sound") from start to finish. ``done`` carries the
    film's path in the library, or None if it was cancelled or failed."""
    done = Signal(object)

    def __init__(self, controller: "LibraryController", source: Path, convert_only: bool = False):
        super().__init__(controller)
        self.c = controller
        self.window = controller.window
        self.source = source
        self.convert_only = convert_only
        self.dialog = JobDialog("Convert the sound" if convert_only else "Add film", self.window)
        self.dialog.cancelClicked.connect(self.cancel)
        self.worker: Optional[QObject] = None
        self.target: Optional[Path] = None
        self.probe: dict = {}
        self.eta: Optional[Eta] = None
        self._convert_error = ""
        self._all_audio = False
        self._reason = ""
        self._player_detail = ""
        self._finished = False

    # ---- steps
    def start(self) -> None:
        folder = self.c.folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.report("SW-221", f"can't create {folder}: {errors.describe_exception(exc)}")
            self._finish(None)
            return
        if not self.convert_only:
            if library.is_inside(folder, self.source):
                self.window.activity(f"{self.source.name} is already in your library", "you")
                self._finish(self.source)
                return
            copy = library.find_copy(folder, self.source)
            if copy is not None:
                self.window.activity(f"{self.source.name} is already in your library as {copy.name}", "you")
                self._finish(copy)
                return
        self.dialog.show()
        self.dialog.set_step(f"Checking {self.source.name}…", busy=True,
                             hint="Reading which picture and sound tracks it has. This takes a few seconds.")
        probe = ConverterProcess(["probe", str(self.source)], self)
        probe.message.connect(self._probe_message)
        probe.finished.connect(self._probe_finished)
        self.worker = probe
        QTimer.singleShot(PROBE_LIMIT_MS, probe, lambda: self._probe_timeout(probe))
        probe.start()

    def _probe_message(self, message: dict) -> None:
        if isinstance(message.get("probe"), dict):
            self.probe = message["probe"]
        elif message.get("error"):
            self.probe = {"error": str(message.get("detail", ""))}

    def _probe_timeout(self, probe: ConverterProcess) -> None:
        if self.worker is probe and not self._finished:
            probe.kill()

    def _probe_finished(self, code: int, error_output: str) -> None:
        if self._finished:
            return
        probe = self.probe
        if not probe:
            if "No module named 'av'" in error_output:
                errors.report("SW-225", error_output.strip()[-300:])
                self._finish(None)
                return
            probe = {"error": f"exit code {code}: {error_output.strip()[-300:] or '-'}"}
        if probe.get("error"):
            self._cant_play("SyncWatch couldn't read this file. It may be damaged, not fully downloaded, or not a "
                            "film at all.", probe["error"])
            return
        if not probe.get("video_ok"):
            self._cant_play("SyncWatch couldn't read a picture from this file. It may be damaged, not fully "
                            "downloaded, or a format no player here supports.", json.dumps(probe)[:1500])
            return
        if self.convert_only:
            self._offer_conversion("manual")
            return
        self.dialog.set_step(f"Checking that {self.source.name} plays…", busy=True,
                             hint="Playing it invisibly for a moment with SyncWatch's video player.")
        test = PlayabilityTest(self.window.player.engine, self.source, self)
        test.finished.connect(self._tested)
        self.worker = test
        test.start()

    def _tested(self, result: dict) -> None:
        if self._finished:
            return
        if not result.get("picture") and result.get("detail") == "timeout":
            # A very slow drive, not a broken film: the converter already read a picture from it.
            log.info("the test playback of %s ran out of time; adding it anyway", self.source.name)
            self._copy()
        elif not result.get("picture"):
            # The converter could read the picture, the player couldn't play the file: often the
            # file's packaging (container) is the problem, and a fresh copy fixes it.
            self._player_detail = result.get("detail", "")
            self._offer_conversion("player")
        elif result.get("sound") is False:
            self._offer_conversion("sound")
        else:
            self._copy()

    def _source_size(self) -> Optional[int]:
        try:
            return self.source.stat().st_size
        except OSError as exc:
            errors.report("SW-204", errors.describe_exception(exc))
            self._finish(None)
            return None

    def _copy(self) -> None:
        size = self._source_size()
        if size is None or not self._space_for(size):
            return
        self.target = library.unique_path(self.c.folder(), self.source.name)
        self.dialog.set_step(f"Copying {self.source.name} into your library…", busy=False,
                             hint="The original stays where it is. You can keep watching meanwhile.")
        self.eta = Eta(size)
        worker = CopyWorker(self.source, self.target, self)
        worker.progress.connect(self._copy_progress)
        worker.finished.connect(self._copied)
        self.worker = worker
        worker.start()

    def _copy_progress(self, done: int, total: int) -> None:
        speed, left = self.eta.update(done)
        speed_text = f" · {human_size(speed)}/s" if speed else ""
        self.dialog.set_progress(done / total if total else 0.0,
                                 f"{human_size(done)} of {human_size(total)}{speed_text} · {human_time(left)}")

    def _copied(self, problem) -> None:
        if self._finished:
            return
        if problem is None:
            self.window.activity(f"Added {self.target.name} to your library", "you")
            self._finish(self.target)
        elif isinstance(problem, library.Cancelled):
            self._finish(None)
        else:
            errors.report("SW-221", errors.describe_exception(problem))
            self._finish(None)

    def _offer_conversion(self, reason: str) -> None:
        """reason: "manual" (Convert the sound…), "sound" (the picture plays, the sound doesn't),
        or "player" (the player can't play the file as it is, although its picture can be read)."""
        self._reason = reason
        tracks = [t for t in self.probe.get("tracks", []) if t.get("type") == "audio"]
        to_convert = [t for t in tracks if not _keeps_audio(str(t.get("codec", "")))]
        manual = reason == "manual"
        if manual and not to_convert:
            self.dialog.hide()
            _message(self.window, "Convert the sound",
                     f"The sound of {self.source.name} is already in a format every player plays (AAC, MP3, Opus, "
                     "Vorbis, FLAC or PCM). Converting wouldn't change anything.")
            self._finish(None)
            return
        if not tracks and reason == "sound":
            self._copy()   # no sound track at all: nothing to convert, the picture plays
            return
        # The player can't play a sound that's already AAC (or similar), or the file at all? Then
        # every sound track is converted.
        self._all_audio = reason == "player" or not to_convert
        names = ", ".join(sorted({_sound_name(str(t.get("codec", ""))) for t in (to_convert or tracks)}))
        minutes = max(1, round(float(self.probe.get("duration") or 0) / 60 / 25))
        if reason == "manual":
            first = f"The sound of {self.source.name} is {names}."
        elif reason == "sound":
            first = f"This film's picture plays, but SyncWatch can't play its sound ({names})."
        else:
            first = (f"SyncWatch's video player can't play {self.source.name} as it is. A fresh copy often "
                     "fixes that.")
        question = (f"{first}\n\nSyncWatch can make a copy that plays everywhere: the picture is copied "
                    "exactly as it is, only the sound is converted (to AAC). Subtitles inside the film are kept, "
                    f"and the original file stays as it is.\n\nThis takes roughly {minutes} "
                    f"minute{'s' if minutes > 1 else ''} (it depends on the PC). Convert it?")
        self.dialog.hide()
        title = "Make a playable copy" if reason == "player" else "Convert the sound"
        box = QMessageBox(QMessageBox.Icon.Question, title, question, parent=self.window)
        box.setTextFormat(Qt.TextFormat.PlainText)
        yes = box.addButton("Convert", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not yes:
            self._finish(None)
            return
        self._convert()

    def _convert(self) -> None:
        size = self._source_size()
        if size is None or not self._space_for(size):
            return
        stem = self.source.stem + (" (converted)" if self.convert_only else "")
        self.target = library.unique_path(self.c.folder(), stem + ".mkv")
        self.dialog.show()
        self.dialog.set_step(f"Converting the sound of {self.source.name}…", busy=False,
                             hint="The picture is copied untouched. The original file stays as it is.")
        duration = float(self.probe.get("duration") or 0.0)
        self.eta = Eta(duration or 1.0, warmup=3.0)
        process = ConverterProcess(["convert", str(self.source), str(self.target)]
                                   + (["--all-audio"] if self._all_audio else []), self)
        process.message.connect(self._convert_message)
        process.finished.connect(self._converted)
        self.worker = process
        self._convert_error = ""
        process.start()

    def _convert_message(self, message: dict) -> None:
        if "progress" in message:
            done_s = float(message.get("done_s") or 0.0)
            _rate, left = self.eta.update(done_s)
            self.dialog.set_progress(float(message["progress"]),
                                     f"{fmt_time(done_s * 1000)} of {fmt_time(self.eta.total * 1000)} · "
                                     f"{human_time(left)}")
        elif message.get("error"):
            self._convert_error = str(message.get("detail", ""))

    def _converted(self, code: int, error_output: str) -> None:
        if self._finished:
            return
        if code == 0 and self.target is not None and self.target.is_file():
            if self._reason == "player":
                self._retest()   # does the fresh copy play? Otherwise it's not kept
                return
            self.window.activity(f"Converted the sound: {self.target.name} is in your library", "you")
            self._finish(self.target)
            return
        if self._convert_error or code != -1:
            detail = self._convert_error or error_output.strip()[-500:] or f"exit code {code}"
            if "No module named 'av'" in detail:
                errors.report("SW-225", detail)
            else:
                errors.report("SW-222", detail)
        self._cleanup_part()
        self._finish(None)

    def _retest(self) -> None:
        self.dialog.set_step(f"Checking that {self.target.name} plays…", busy=True)
        test = PlayabilityTest(self.window.player.engine, self.target, self)
        test.finished.connect(self._retested)
        self.worker = test
        test.start()

    def _retested(self, result: dict) -> None:
        if self._finished:
            return
        if result.get("picture"):
            self.window.activity(f"Made a playable copy: {self.target.name} is in your library", "you")
            self._finish(self.target)
            return
        try:
            self.target.unlink()
        except OSError:
            pass
        self._cant_play("SyncWatch's video player can't play this film, not even a fresh copy of it.",
                        f"{self._player_detail} / copy: {result.get('detail', '')}")

    # ---- helpers
    def _space_for(self, size: int) -> bool:
        try:
            problem = library.space_problem(self.c.folder(), size)
        except OSError as exc:
            errors.report("SW-221", errors.describe_exception(exc))
            self._finish(None)
            return False
        if problem is not None:
            free, needed = problem
            errors.report("SW-220", free=human_size(free), needed=human_size(needed))
            self._finish(None)
            return False
        return True

    def _cant_play(self, reason: str, detail: str) -> None:
        self.dialog.hide()
        errors.report("SW-223", detail or "-", reason=reason)
        self._finish(None)

    def _cleanup_part(self) -> None:
        if self.target is not None:
            part = self.target.with_name(self.target.name + ".part")
            try:
                part.unlink()
            except OSError:
                pass

    def cancel(self) -> None:
        if self._finished:
            return
        worker = self.worker
        if isinstance(worker, CopyWorker):
            worker.cancel()   # it reports back (and deletes its .part file) by itself
            self.dialog.set_step("Cancelling…", busy=True)
            return
        if isinstance(worker, ConverterProcess):
            worker.kill()
            self._cleanup_part()
        self._finish(None)

    def _finish(self, result: Optional[Path]) -> None:
        if self._finished:
            return
        self._finished = True
        self.dialog.hide()
        self.dialog.deleteLater()
        self.done.emit(result)


def _message(parent, title: str, text: str) -> None:
    box = QMessageBox(QMessageBox.Icon.Information, title, text, parent=parent)
    box.setTextFormat(Qt.TextFormat.PlainText)   # file names are shown as they are, never as HTML
    box.exec()


def _keeps_audio(codec: str) -> bool:
    return codec in {"aac", "mp3", "opus", "vorbis", "flac"} or codec.startswith("pcm_")


def _sound_name(codec: str) -> str:
    return SOUND_CODECS.get(codec, codec.upper() or "unknown")


# =============================================================== the library for the window
class LibraryController(QObject):
    """Everything library: keeps the lists up to date and runs Add film / Remove / Convert."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.settings = window.settings
        self.panels: List[LibraryPanel] = []
        self.dialog: Optional[LibraryDialog] = None
        self.job: Optional[AddFilmJob] = None
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(lambda _path: self._refresh_timer.start())
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(400)
        self._refresh_timer.timeout.connect(self.refresh)
        self.settings.subscribe("paths.movies_dir", lambda key, value: self._folder_changed())
        self._folder_changed()

    def folder(self) -> Path:
        return paths.movies_dir(self.settings.get("paths.movies_dir"))

    def _folder_changed(self) -> None:
        folder = self.folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        library.remove_stale_parts(folder)
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        if folder.is_dir():
            self.watcher.addPath(str(folder))
        self.refresh()

    def attach(self, panel: LibraryPanel) -> None:
        panel.playRequested.connect(self.play)
        panel.addRequested.connect(self.add_film)
        panel.openFileRequested.connect(self.open_file)
        panel.showRequested.connect(self.show_in_folder)
        panel.convertRequested.connect(self.convert_film)
        panel.removeRequested.connect(self.remove_film)
        self.panels.append(panel)
        self.refresh()

    def refresh(self) -> None:
        folder = self.folder()
        films = library.list_films(folder)
        for panel in self.panels:
            panel.set_films(films, folder, self._watched)

    def _watched(self, film: library.Film) -> str:
        position = int(self.window.films.get(film_key(film.name, film.size)).get("pos_ms", 0))
        return f"stopped at {fmt_time(position)}" if position > 0 else ""

    # ---- actions
    def open_dialog(self) -> None:
        if self.dialog is None:
            self.dialog = LibraryDialog(self.window)
            self.attach(self.dialog.panel)
        self.refresh()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def play(self, path: Optional[Path]) -> None:
        if path is None:
            return
        if self.dialog is not None:
            self.dialog.hide()
        self.window.open_film(path)

    def open_file(self) -> None:
        start = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation) or str(self.folder())
        path, _ = QFileDialog.getOpenFileName(self.window, "Play a film without adding it", start,
                                              f"Films ({FILM_PATTERNS});;All files (*)")
        if path:
            self.play(Path(path))

    def add_film(self) -> None:
        if self.job is not None:
            self.job.dialog.show()
            self.job.dialog.raise_()
            return
        start = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation) or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self.window, "Add a film to your library", start,
                                              f"Films ({FILM_PATTERNS});;All files (*)")
        if path:
            self.start_job(Path(path))

    def start_job(self, source: Path, convert_only: bool = False) -> None:
        """Add ``source`` to the library (or convert its sound), then open it."""
        if self.job is not None:
            return
        self.job = AddFilmJob(self, source, convert_only)
        self.job.done.connect(self._job_done)
        self.job.start()

    def _job_done(self, result: Optional[Path]) -> None:
        if self.job is not None:
            self.job.deleteLater()
        self.job = None
        self.refresh()
        if result is not None:
            self.play(result)

    def convert_film(self, path: Optional[Path]) -> None:
        if path is not None:
            self.start_job(path, convert_only=True)

    def show_in_folder(self, path: Optional[Path]) -> None:
        if path is not None and sys.platform == "win32" and path.is_file():
            subprocess.Popen(f'explorer /select,"{path}"')   # opens the folder with the film selected
            return
        folder = path.parent if path is not None else self.folder()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def remove_film(self, path: Optional[Path]) -> None:
        if path is None:
            return
        if not library.is_inside(self.folder(), path):
            return   # only films in the library folder are ever removed
        playing = self.window.player.path is not None and self.window.player.path == path
        text = (f"Remove {path.name} from your library?\n\nIt goes to the Recycle Bin, so you can still get it back. "
                + ("It's open right now, so it closes first." if playing else ""))
        if not self.window._confirm("Remove film?", text, "Remove"):
            return
        if playing:
            self.window.close_film()
            # The player and the subtitle reader let go of the file a moment later.
            QTimer.singleShot(REMOVE_AFTER_CLOSE_MS, self, lambda: self._remove_now(path))
        else:
            self._remove_now(path)

    def _remove_now(self, path: Path) -> None:
        if not QFile.moveToTrash(str(path)):
            try:
                path.unlink()
            except OSError as exc:
                errors.report("SW-224", errors.describe_exception(exc))
                return
        self.window.activity(f"Removed {path.name} from your library", "you")
        self.refresh()

    def shutdown(self) -> None:
        """The window is closing: stop a running job (its partial file is deleted)."""
        if self.job is not None:
            self.job.cancel()
