"""What should we watch? The Discover window and the film vote panel (0.5).

The window shows films as posters: "For you" (recommendations from the films in your library and
the ones you watched), "Trending" (this week on TMDB), "Your films" (the library, matched to TMDB)
and search results. Clicking one shows its details: rating, length, genres, the description, why
it's suggested, where to watch it, the trailer and, in a room, "Suggest for the vote".

Without a TMDB key, "Your films" still works (titles come from the file names) and so does the
vote; a note says how to get a key. TMDB is asked in a background thread (TmdbWorker), posters
after everything else, and the answers are cached (discover/tmdb.py).

The vote panel (in the sidebar, in a room): the suggested films with their votes and who has them;
double-click to vote, the host closes the vote, and the winner can be played straight away by
whoever has it.
"""
from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QPoint, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListView, QListWidget, QListWidgetItem, QMenu, QPushButton, QScrollArea,
                               QSizePolicy, QSplitter, QVBoxLayout, QWidget)

from .. import errors
from ..discover import names, picks, tmdb
from ..discover.vote import Choice
from ..filmstate import film_key
from ..library import films as library

log = logging.getLogger("syncwatch.discover")

MAX_SEEDS = 25            # your films looked at for "For you" (the most recently watched first)
POSTER = QSize(120, 180)
GRID = QSize(142, 238)
KEY_HELP = ("Recommendations, posters and trailers come from TMDB (The Movie Database). It's free: "
            "sign up at themoviedb.org, open Settings > API, ask for a key (personal use), then copy the "
            "\"API Read Access Token\" into Settings > Ideas > TMDB key.")
def relay_help() -> str:
    problem = errors.problem("SW-636")
    return f"{problem.title}. {problem.explanation} {problem.tips[0]}"


PROBLEMS = {"nokey": "SW-631", "key": "SW-632", "network": "SW-633", "busy": "SW-634", "odd": "SW-635",
            "missing": "SW-635", "relay": "SW-636"}
RELAY_RETRY_S = 600          # after the relay said it can't ask TMDB, try it again this much later


@dataclass
class Card:
    """One film in the grid: from TMDB (movie), or only from a file name."""
    title: str
    year: int = 0
    movie: Optional[tmdb.Movie] = None
    path: Optional[Path] = None                       # it's in my library
    because: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


# ================================================================ the background thread
class TmdbWorker(QObject):
    """TMDB questions, one after another in a background thread (posters only when nothing else
    waits). finished(tag, result) / failed(tag, kind) / progress(tag, done, total) arrive in the GUI
    thread. reset() (new settings) drops what's waiting and starts with a fresh client."""
    finished = Signal(str, object)
    failed = Signal(str, str)
    progress = Signal(str, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._params: Optional[tuple] = None
        self._jobs: "queue.PriorityQueue" = queue.PriorityQueue()
        self._count = itertools.count()
        self._generation = 0
        self._client: Optional[tmdb.Client] = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="tmdb", daemon=True)
        self._thread.start()

    def ask(self, tag: str, job: Callable[..., Any], poster: bool = False) -> None:
        self._jobs.put((1 if poster else 0, next(self._count), self._generation, tag, job))

    def configure(self, key: str, folder: Path, language: str, region: str, relay: str = "") -> None:
        """Settings for the client (asked for in the GUI thread); drops what's waiting.
        relay: SyncWatch's relay, which asks TMDB when there's no key of one's own (since 0.5.1)."""
        with self._lock:
            self._generation += 1
            self._params = (key, folder, language, region)
            self._relay = relay
            self._client = None

    def stop(self) -> None:
        self._jobs.put((-1, next(self._count), -1, "", None))

    def _run(self) -> None:
        while True:
            _priority, _number, generation, tag, job = self._jobs.get()
            if job is None:
                break
            with self._lock:
                if generation != self._generation:
                    continue          # asked before the settings changed
                if self._client is None:
                    self._client = tmdb.Client(*self._params, relay=getattr(self, "_relay", ""))
                client = self._client
            try:
                result = job(client, lambda done, total, t=tag: self.progress.emit(t, done, total))
            except tmdb.TmdbError as exc:
                log.info("TMDB: %s (%s)", exc.kind, tag.split(":")[0])
                self.failed.emit(tag, exc.kind)
            except Exception:  # noqa: BLE001 - a bug here must not end the thread
                log.exception("TMDB job %s failed", tag.split(":")[0])
                self.failed.emit(tag, "odd")
            else:
                self.finished.emit(tag, result)
            finally:
                if self._jobs.empty():
                    client.save()


def for_you(client: tmdb.Client, films: List[Tuple[str, float]], report: Callable[[int, int], None]) -> Tuple[List[picks.Pick], Dict[str, tmdb.Movie]]:
    """(recommendations, my films matched to TMDB by file name). films: (file name, watched at)."""
    now, seeds, matched, seen = time.time(), [], {}, set()
    todo = [(name, watched, names.guess(name)) for name, watched in films]
    todo = [entry for entry in todo if entry[2] is not None][:MAX_SEEDS]
    for done, (name, watched, guess) in enumerate(todo):
        report(done, len(todo) * 2)
        if guess.key in seen:
            continue
        seen.add(guess.key)
        try:
            movie = client.details(guess.tmdb).movie if guess.tmdb else client.find(guess.title, guess.year)
        except tmdb.TmdbError as exc:
            if exc.kind not in ("missing", "odd"):
                raise
            movie = None
        if movie is not None:
            matched[name] = movie
            seeds.append(picks.Seed(movie, picks.seed_weight(watched, now)))
    recommendations = {}
    for done, seed in enumerate(seeds):
        report(len(todo) + done, len(todo) + len(seeds))
        try:
            recommendations[seed.movie.id] = list(client.details(seed.movie.id).recommendations)
        except tmdb.TmdbError as exc:
            if exc.kind not in ("missing", "odd"):
                raise
    return picks.combine(seeds, recommendations), matched


# ================================================================ small pictures
def placeholder(title: str, size: QSize = POSTER) -> QPixmap:
    """A dark card with the title's first letters, until (or instead of) the poster."""
    pixmap = QPixmap(size)
    pixmap.fill(QColor("#232832"))
    painter = QPainter(pixmap)
    painter.setPen(QColor("#8b93a7"))
    font = QFont()
    font.setPointSize(22)
    font.setBold(True)
    painter.setFont(font)
    letters = "".join(word[0] for word in title.split()[:2] if word[:1].isalnum()).upper() or "?"
    painter.drawText(pixmap.rect(), int(Qt.AlignmentFlag.AlignCenter), letters)
    painter.end()
    return pixmap


def poster_pixmap(data: bytes, size: QSize = POSTER) -> Optional[QPixmap]:
    pixmap = QPixmap()
    if not data or not pixmap.loadFromData(data):
        return None
    scaled = pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    return scaled.copy((scaled.width() - size.width()) // 2, (scaled.height() - size.height()) // 2,
                       size.width(), size.height())


def watch_link(movie_id: int) -> str:
    return f"https://www.themoviedb.org/movie/{int(movie_id)}/watch"


def open_link(link: str) -> None:
    """Only TMDB's and YouTube's own pages are ever opened from here."""
    if tmdb.WATCH_LINK.match(link) or link.startswith("https://www.youtube.com/watch?v="):
        QDesktopServices.openUrl(QUrl(link))


# ================================================================ the window
class DiscoverWindow(QDialog):
    """Signals: trailerRequested(video ID), suggestRequested(title, year, tmdb, poster), playRequested(path)."""
    trailerRequested = Signal(str)
    suggestRequested = Signal(str, int, int, str)
    playRequested = Signal(object)
    pickedForMe = Signal(object)

    TABS = (("you", "For you"), ("trending", "Trending"), ("mine", "Your films"))

    def __init__(self, discover: "Discover", parent=None):
        super().__init__(parent)
        self.discover = discover
        self.setWindowTitle("What should we watch?")
        self.resize(1080, 700)
        self.setModal(False)
        self.tab = "you"
        self.cards: Dict[str, List[Card]] = {"you": [], "trending": [], "mine": [], "search": []}
        self.current: Optional[Card] = None
        self.details: Dict[int, tmdb.Details] = {}
        self._items: Dict[str, List[QListWidgetItem]] = {}     # poster path -> items showing it

        self.buttons = QButtonGroup(self)
        top = QHBoxLayout()
        for name, text in self.TABS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setChecked(name == self.tab)
            button.clicked.connect(lambda _=False, n=name: self.show_tab(n))
            self.buttons.addButton(button)
            top.addWidget(button)
        top.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a film…")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(260)
        self.search.returnPressed.connect(self._search)
        self.surprise = QPushButton("🎲  Pick one for me")
        self.surprise.setToolTip("One of the best few in \"For you\", at random")
        self.surprise.clicked.connect(self._surprise)
        top.addWidget(self.search)
        top.addWidget(self.surprise)

        self.note = QLabel("")
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setObjectName("Hint")
        self.note.setWordWrap(True)
        self.note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.grid = QListWidget()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setIconSize(POSTER)
        self.grid.setGridSize(GRID)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setWordWrap(True)
        self.grid.setUniformItemSizes(True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.currentItemChanged.connect(lambda item, _old: self._select(item))
        self.grid.itemDoubleClicked.connect(lambda item: self._double_clicked(item))

        self.side = self._build_side()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.grid)
        splitter.addWidget(self.side)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([740, 340])
        footer = QLabel(f"{tmdb.NOTICE} Where-to-watch data: JustWatch.")
        footer.setObjectName("Hint")
        footer.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.note)
        layout.addWidget(splitter, 1)
        layout.addWidget(footer)
        self._show_card(None)

    def _build_side(self) -> QWidget:
        side = QFrame()
        side.setObjectName("Card")
        side.setMinimumWidth(300)
        self.poster = QLabel()
        self.poster.setFixedSize(QSize(154, 231))
        self.poster.setScaledContents(True)
        self.title = QLabel("")
        self.title.setObjectName("LibraryTitle")
        self.title.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setObjectName("Dim")
        self.meta.setWordWrap(True)
        self.why = QLabel("")
        self.why.setWordWrap(True)
        self.tagline = QLabel("")
        self.tagline.setObjectName("Dim")
        self.tagline.setWordWrap(True)
        self.overview = QLabel("")
        self.overview.setWordWrap(True)
        self.overview.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.overview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.where = QLabel("")
        self.where.setObjectName("Dim")
        self.where.setWordWrap(True)
        self.trailer_button = QPushButton("▶  Trailer")
        self.trailer_button.clicked.connect(self._trailer)
        self.where_button = QPushButton("Where to watch")
        self.where_button.clicked.connect(self._where)
        self.play_button = QPushButton("▶  Play")
        self.play_button.setObjectName("Accent")
        self.play_button.clicked.connect(lambda: self.current and self.current.path
                                         and self.playRequested.emit(self.current.path))
        self.suggest_button = QPushButton("🗳  Suggest for the vote")
        self.suggest_button.setObjectName("Accent")
        self.suggest_button.setToolTip("Puts this film into the room's film vote (starts one if none is on)")
        self.suggest_button.clicked.connect(self._suggest)
        row = QHBoxLayout()
        row.addWidget(self.trailer_button)
        row.addWidget(self.where_button)
        text = QWidget()
        text_layout = QVBoxLayout(text)
        text_layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.title, self.meta, self.why, self.tagline, self.overview, self.where):
            text_layout.addWidget(widget)
        text_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(text)
        for label in (self.title, self.meta, self.why, self.tagline, self.overview, self.where):
            label.setTextFormat(Qt.TextFormat.PlainText)     # titles and names from TMDB and friends: never HTML
        layout = QVBoxLayout(side)
        layout.addWidget(self.poster, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(scroll, 1)
        layout.addLayout(row)
        layout.addWidget(self.play_button)
        layout.addWidget(self.suggest_button)
        return side

    # ------------------------------------------------------------ what's shown
    def show_tab(self, name: str) -> None:
        self.tab = name
        for button, (tab, _text) in zip(self.buttons.buttons(), self.TABS):
            button.setChecked(tab == name)
        self.discover.tab_opened(name)
        self.fill()

    def set_cards(self, tab: str, cards: List[Card]) -> None:
        self.cards[tab] = cards
        if tab == self.tab:
            self.fill()

    def set_note(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def fill(self) -> None:
        selected = self.current.movie.id if self.current and self.current.movie else None
        self.grid.clear()
        self._items.clear()
        for card in self.cards.get(self.tab, []):
            rating = f"★ {card.movie.rating:.1f}" if card.movie and card.movie.votes else ""
            line = " · ".join(part for part in (str(card.year) if card.year else "", rating) if part)
            item = QListWidgetItem(QIcon(placeholder(card.title)), f"{card.title}\n{line}" if line else card.title)
            item.setData(Qt.ItemDataRole.UserRole, card)
            tip = card.label + (f"\nBecause you have {' and '.join(card.because)}" if card.because else "")
            if card.movie and card.movie.overview:
                tip += "\n\n" + card.movie.overview
            item.setToolTip(tip)
            self.grid.addItem(item)
            if card.movie and card.movie.poster:
                self._items.setdefault(card.movie.poster, []).append(item)
                self.discover.want_poster(card.movie.poster)
            if selected is not None and card.movie and card.movie.id == selected:
                self.grid.setCurrentItem(item)
        self.surprise.setVisible(self.tab == "you" and bool(self.cards["you"]))
        if self.grid.currentItem() is None:
            self._show_card(None)

    def poster_arrived(self, path: str, pixmap: QPixmap) -> None:
        for item in self._items.get(path, []):
            item.setIcon(QIcon(pixmap))
        if self.current and self.current.movie and self.current.movie.poster == path:
            self.poster.setPixmap(pixmap)

    def details_arrived(self, details: tmdb.Details) -> None:
        self.details[details.movie.id] = details
        if self.current and self.current.movie and self.current.movie.id == details.movie.id:
            self._show_card(self.current)

    def select_movie(self, movie_id: int) -> None:
        for row in range(self.grid.count()):
            card = self.grid.item(row).data(Qt.ItemDataRole.UserRole)
            if card.movie and card.movie.id == movie_id:
                self.grid.setCurrentRow(row)
                self.grid.scrollToItem(self.grid.item(row))
                return

    def refresh_buttons(self) -> None:
        self._show_card(self.current)

    def _select(self, item: Optional[QListWidgetItem]) -> None:
        card = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        self._show_card(card)
        if card is not None and card.movie and card.movie.id not in self.details:
            self.discover.want_details(card.movie.id)

    def _show_card(self, card: Optional[Card]) -> None:
        self.current = card
        for widget in (self.trailer_button, self.where_button, self.play_button, self.suggest_button):
            widget.setVisible(False)
        if card is None:
            self.poster.setPixmap(placeholder("", QSize(154, 231)))
            self.title.setText("Pick a film")
            for label in (self.meta, self.why, self.tagline, self.where):
                label.setText("")
            self.overview.setText("Click a film to see what it's about, why it's suggested, where to watch it "
                                  "and its trailer.")
            return
        movie = card.movie
        details = self.details.get(movie.id) if movie else None
        self.title.setText(card.label)
        pixmap = self.discover.poster_for(movie.poster) if movie and movie.poster else None
        self.poster.setPixmap(pixmap or placeholder(card.title, QSize(154, 231)))
        meta = []
        if details and details.runtime:
            meta.append(f"{details.runtime // 60} h {details.runtime % 60:02d} min" if details.runtime >= 60
                        else f"{details.runtime} min")
        if movie and movie.votes:
            meta.append(f"★ {movie.rating:.1f} ({movie.votes:,} ratings)".replace(",", " "))
        if movie and movie.genres:
            meta.append(", ".join(movie.genres[:3]))
        self.meta.setText("  ·  ".join(meta))
        why = []
        if card.because:
            why.append("Because you have " + " and ".join(card.because))
        if card.path is not None:
            why.append("In your library")
        elif self.discover.have_film(card.title, card.year, movie.id if movie else 0) is not None:
            why.append("In your library")
        self.why.setText(" · ".join(why))
        self.tagline.setText(details.tagline if details else "")
        if movie:
            self.overview.setText(movie.overview or "No description on TMDB.")
        else:
            self.overview.setText(f"Not found on TMDB. ({card.path.name})" if card.path else "Not found on TMDB.")
        providers = details.providers if details else None
        self.where.setText(providers.summary() + f" ({providers.region})" if providers and providers.summary() else
                           ("Not streaming anywhere in your country that TMDB knows of." if providers else ""))
        self.trailer_button.setVisible(bool(details and details.trailer))
        self.where_button.setVisible(bool(movie))
        mine = card.path or self.discover.have_film(card.title, card.year, movie.id if movie else 0)
        self.play_button.setVisible(mine is not None)
        self.suggest_button.setVisible(self.discover.in_room())

    # ------------------------------------------------------------ buttons
    def _trailer(self) -> None:
        details = self.details.get(self.current.movie.id) if self.current and self.current.movie else None
        if details and details.trailer:
            self.trailerRequested.emit(details.trailer)

    def _where(self) -> None:
        if not self.current or not self.current.movie:
            return
        details = self.details.get(self.current.movie.id)
        link = details.providers.link if details and details.providers else watch_link(self.current.movie.id)
        open_link(link)

    def _suggest(self) -> None:
        card = self.current
        if card is not None:
            movie = card.movie
            self.suggestRequested.emit(movie.title if movie else card.title, card.year, movie.id if movie else 0,
                                       movie.poster if movie else "")

    def _double_clicked(self, item: QListWidgetItem) -> None:
        card = item.data(Qt.ItemDataRole.UserRole)
        if self.discover.in_room():
            self.grid.setCurrentItem(item)
            self._suggest()
        elif card.path is not None:
            self.playRequested.emit(card.path)

    def _search(self) -> None:
        text = " ".join(self.search.text().split())
        if not text:
            return
        self.tab = "search"
        for button in self.buttons.buttons():
            button.setChecked(False)
        self.cards["search"] = []
        self.fill()
        self.discover.search(text)

    def _surprise(self) -> None:
        pick = picks.surprise([picks.Pick(card.movie, 1.0 / (rank + 1)) for rank, card in
                               enumerate(self.cards["you"]) if card.movie])
        if pick is not None:
            self.show_tab("you")
            self.select_movie(pick.movie.id)
            self.pickedForMe.emit(pick.movie)


# ================================================================ the vote panel (sidebar)
class VotePanel(QFrame):
    """The room's film vote: double-click a film to vote (again: take the vote back)."""
    voteRequested = Signal(int)
    withdrawRequested = Signal(int)
    suggestClicked = Signal()
    closeClicked = Signal()
    cancelClicked = Signal()
    playClicked = Signal(int)
    whereClicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Panel")
        body = QVBoxLayout(self)
        body.setContentsMargins(10, 8, 10, 10)
        body.setSpacing(6)
        self.heading = QLabel("FILM VOTE")
        self.heading.setObjectName("PanelTitle")
        self.status = QLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)    # friends' names and titles: never HTML
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.itemDoubleClicked.connect(
            lambda item: self.open and self.voteRequested.emit(int(item.data(Qt.ItemDataRole.UserRole))))
        self.suggest_button = QPushButton("➕ Suggest…")
        self.suggest_button.setToolTip("Find a film to suggest (What should we watch?)")
        self.suggest_button.clicked.connect(self.suggestClicked)
        self.close_button = QPushButton("Close the vote")
        self.close_button.setObjectName("Accent")
        self.close_button.setToolTip("The film with the most votes wins")
        self.close_button.clicked.connect(self.closeClicked)
        self.cancel_button = QPushButton("✕")
        self.cancel_button.setToolTip("Cancel the vote (host)")
        self.cancel_button.setFixedWidth(32)
        self.cancel_button.clicked.connect(self.cancelClicked)
        self.play_button = QPushButton("▶ Play it")
        self.play_button.setObjectName("Accent")
        self.play_button.clicked.connect(lambda: self.playClicked.emit(self.winner))
        self.where_button = QPushButton("Where to watch")
        self.where_button.clicked.connect(lambda: self.whereClicked.emit(self.winner))
        buttons = QHBoxLayout()
        for button in (self.suggest_button, self.close_button, self.play_button, self.where_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        body.addWidget(self.heading)
        body.addWidget(self.status)
        body.addWidget(self.list)
        body.addLayout(buttons)
        self.open = False
        self.winner = 0
        self.choices: List[Choice] = []
        self._mine: Dict[int, bool] = {}

    def update_vote(self, open_: bool, choices: List[Choice], winner: int, my_vote: int, my_name: str,
                    host: bool, have: List[int]) -> None:
        self.open, self.winner, self.choices = open_, winner, list(choices)
        self._mine = {c.key: c.by == my_name for c in choices}
        self.list.clear()
        shown = choices if open_ else [c for c in choices if c.key == winner]
        for choice in shown:
            votes = len(choice.voters)
            mark = "✓ " if choice.key == my_vote else ""
            badge = f"   🗳 {votes}" if votes else ""
            item = QListWidgetItem(f"{mark}{choice.label}{badge}")
            item.setData(Qt.ItemDataRole.UserRole, choice.key)
            if choice.key == my_vote:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            tip = [f"Suggested by {choice.by or 'someone'}"]
            if choice.voters:
                tip.append("Votes: " + ", ".join(choice.voters))
            if choice.have:
                tip.append("Have it: " + ", ".join(choice.have))
            if choice.key in have:
                tip.append("You have it in your library")
            item.setToolTip("\n".join(tip))
            self.list.addItem(item)
        if shown:
            row = self.list.sizeHintForRow(0)
            row = row if row > 0 else self.fontMetrics().height() + 6
            self.list.setFixedHeight(row * min(len(shown), 6) + 2 * self.list.frameWidth() + 8)
        self.list.setVisible(bool(shown))
        won = next((c for c in choices if c.key == winner), None) if not open_ else None
        if open_:
            voted = sum(len(c.voters) for c in choices)
            self.status.setText("Suggest films and vote for tonight's (double-click a film). "
                                + (f"{voted} vote{'s' if voted != 1 else ''} so far." if choices else
                                   "Nothing suggested yet."))
        elif won is not None:
            who = (", ".join(won.have) + (" has it" if len(won.have) == 1 else " have it")) if won.have else \
                "Nobody has it yet"
            self.status.setText(f"🏆 {won.label} won with {len(won.voters)} vote"
                                f"{'s' if len(won.voters) != 1 else ''}. {who}.")
        self.suggest_button.setVisible(open_)
        self.close_button.setVisible(open_ and host)
        self.cancel_button.setVisible(host)
        self.play_button.setVisible(won is not None and won.key in have)
        self.where_button.setVisible(won is not None and bool(won.tmdb))
        self.heading.setText(f"FILM VOTE ({len(choices)})" if open_ and choices else "FILM VOTE")

    def _menu(self, point: QPoint) -> None:
        item = self.list.itemAt(point)
        if item is None or not self.open:
            return
        key = int(item.data(Qt.ItemDataRole.UserRole))
        menu = QMenu(self)
        menu.addAction("Vote for this film", lambda: self.voteRequested.emit(key))
        if self._mine.get(key) or not self.cancel_button.isHidden():
            menu.addAction("Take it out of the vote", lambda: self.withdrawRequested.emit(key))
        menu.exec(self.list.mapToGlobal(point))


# ================================================================ tying it together
class Discover(QObject):
    """Owns the TMDB thread, the Discover window (made when first opened) and the vote panel's
    contents. The main window creates one and passes vote events from the controller to it."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.settings = window.settings
        self.folder = window.ctx.profile.cache_dir / "tmdb"
        self.worker = TmdbWorker(self)
        self._relay_refused = -1e9                         # when the relay last said it can't ask TMDB
        self._configure()
        self.worker.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self.worker.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        self.worker.progress.connect(self._progress, Qt.ConnectionType.QueuedConnection)
        self.dialog: Optional[DiscoverWindow] = None
        self.matched: Dict[str, tmdb.Movie] = {}          # my films' file names -> TMDB
        self._posters: Dict[str, QPixmap] = {}
        self._asked_posters: set = set()
        self._asked_details: set = set()
        self._guesses: Dict[str, Optional[names.Guess]] = {}
        self._loaded: Dict[str, float] = {}               # tab -> when it was filled
        self._films_cache: Tuple[float, list] = (-1e9, [])
        self.settings.subscribe("discover.", lambda _key, _value: self._settings_changed())

    # ------------------------------------------------------------ settings and the client
    def key(self) -> str:
        return str(self.settings.get("discover.tmdb_key") or "")

    def region(self) -> str:
        region = str(self.settings.get("discover.region") or "").strip().upper()
        if not region:
            from PySide6.QtCore import QLocale
            parts = QLocale.system().name().split("_")
            region = parts[1] if len(parts) > 1 else ""
        return region if len(region) == 2 and region.isalpha() else ""

    def relay(self) -> str:
        """The relay that asks TMDB for me: only without a key of my own, and not for a while after
        it said it can't (it has no key yet, or it's an older relay)."""
        if self.key() or time.monotonic() - self._relay_refused < RELAY_RETRY_S:
            return ""
        from ..room.context import relay_address
        return relay_address(self.settings)

    def can_ask(self) -> bool:
        """TMDB can be asked: with my own key, or through the relay."""
        return bool(tmdb.key_kind(self.key())) or bool(self.relay())

    def _configure(self) -> None:
        self.worker.configure(self.key(), self.folder, str(self.settings.get("discover.language") or "en-US"),
                              self.region(), self.relay())

    def _settings_changed(self) -> None:
        self._configure()
        self._loaded.clear()
        self._asked_details.clear()
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.details.clear()
            self.tab_opened(self.dialog.tab)

    def shutdown(self) -> None:
        self.worker.stop()

    # ------------------------------------------------------------ my films
    def my_films(self) -> List[Tuple[str, float, Optional[Path]]]:
        """(file name, when I last watched it (0: never), path if it's in the library), newest first.
        (Looked at again after a few seconds: the vote asks often.)"""
        at, films = self._films_cache
        if time.monotonic() - at < 3.0:
            return films
        films = self._list_my_films()
        self._films_cache = (time.monotonic(), films)
        return films

    def _list_my_films(self) -> List[Tuple[str, float, Optional[Path]]]:
        folder = self.window.library.folder()
        films, seen = [], set()
        for film in library.list_films(folder):
            used = self.window.films.get(film_key(film.name, film.size)).get("used", 0.0)
            films.append((film.name, float(used), film.path))
            seen.add(film.name)
        if self.settings.get("discover.use_history"):
            for key, entry in self.window.films.entries():
                name, _sep, size = key.rpartition("|")
                if name and name not in seen and size.isdigit() and int(size) > 0:
                    films.append((name, float(entry.get("used", 0.0)), None))
                    seen.add(name)
        films.sort(key=lambda entry: (-entry[1], entry[0].lower()))
        return films

    def guess(self, file_name: str) -> Optional[names.Guess]:
        if file_name not in self._guesses:
            self._guesses[file_name] = names.guess(file_name)
        return self._guesses[file_name]

    def have_film(self, title: str, year: int = 0, movie_id: int = 0) -> Optional[Path]:
        """My library's copy of this film, if I have one (by TMDB number, else title and year)."""
        wanted = names.simplify(title)
        for name, _used, path in self.my_films():
            if path is None:
                continue
            movie = self.matched.get(name)
            if movie_id and movie is not None:
                if movie.id == movie_id:
                    return path
                continue
            guess = self.guess(name)
            if guess and names.simplify(guess.title) == wanted and (not year or not guess.year or guess.year == year):
                return path
        return None

    def have_keys(self, choices: List[Choice]) -> List[int]:
        """For the vote: which of these films I have (nothing if I'd rather not tell)."""
        if not self.settings.get("discover.tell_have"):
            return []
        return [choice.key for choice in choices if self.have_film(choice.title, choice.year, choice.tmdb)]

    # ------------------------------------------------------------ the window
    def in_room(self) -> bool:
        return bool(self.window.controller.connected)

    def open(self, tab: str = "") -> None:
        if self.dialog is None:
            self.dialog = DiscoverWindow(self, self.window)
            self.dialog.trailerRequested.connect(self.window.play_trailer)
            self.dialog.suggestRequested.connect(self._suggest)
            self.dialog.playRequested.connect(self._play)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        wanted = tab or self.dialog.tab
        self.dialog.show_tab(wanted if wanted != "search" else "you")

    def tab_opened(self, tab: str) -> None:
        if self.dialog is None:
            return
        self._note_for_key()
        if tab == "mine" or not self.can_ask():
            self._fill_mine()
        marker = "you" if tab in ("you", "mine") else tab
        if not self.can_ask() or time.monotonic() - self._loaded.get(marker, -1e9) < 600:
            return
        self._loaded[marker] = time.monotonic()
        if marker == "you":
            films = [(name, used) for name, used, _path in self.my_films()]
            self.dialog.set_note("Looking at your films…" if films else
                                 "Add films to your library (or watch some) to get recommendations; meanwhile, "
                                 "here's what's trending.")
            if films:
                self.worker.ask("you", lambda client, report: for_you(client, films, report))
            else:
                self.worker.ask("trending", lambda client, report: client.trending())
        elif tab == "trending":
            self.worker.ask("trending", lambda client, report: client.trending())

    def search(self, text: str) -> None:
        if not self.can_ask():
            self._note_for_key()
            return
        self.dialog.set_note(f"Looking for \"{text}\"…")
        self.worker.ask("search", lambda client, report: client.search(text))

    def want_poster(self, path: str) -> None:
        if path in self._posters:
            if self.dialog is not None:
                self.dialog.poster_arrived(path, self._posters[path])
        elif path not in self._asked_posters and self.can_ask():
            self._asked_posters.add(path)
            self.worker.ask(f"poster:{path}", lambda client, report: client.poster(path), poster=True)

    def poster_for(self, path: str) -> Optional[QPixmap]:
        return self._posters.get(path)

    def want_details(self, movie_id: int) -> None:
        if movie_id not in self._asked_details and self.can_ask():
            self._asked_details.add(movie_id)
            self.worker.ask(f"details:{movie_id}", lambda client, report: client.details(movie_id))

    def _note_for_key(self) -> None:
        if self.dialog is None:
            return
        if self.can_ask():
            if self.dialog.note.text() in (KEY_HELP, relay_help()):
                self.dialog.set_note("")
        elif not self.key():
            self.dialog.set_note(relay_help() if self._relay_refused > 0 else KEY_HELP)
        else:
            self.dialog.set_note("The TMDB key in Settings > Ideas doesn't look right: copy the \"API Read "
                                 "Access Token\" (long) or the \"API Key\" (32 letters and digits) again.")

    def _fill_mine(self) -> None:
        cards = []
        for name, _used, path in self.my_films():
            if path is None:
                continue
            movie, guess = self.matched.get(name), self.guess(name)
            title = movie.title if movie else (guess.title if guess else Path(name).stem)
            year = movie.year if movie else (guess.year if guess else 0)
            cards.append(Card(title, year, movie, path))
        cards.sort(key=lambda card: card.title.lower())
        self.dialog.set_cards("mine", cards)

    # ------------------------------------------------------------ answers from the thread
    def _finished(self, tag: str, result: Any) -> None:
        kind, _sep, rest = tag.partition(":")
        if kind == "poster":
            pixmap = poster_pixmap(result) if result else None
            if pixmap is not None:
                self._posters[rest] = pixmap
                if self.dialog is not None:
                    self.dialog.poster_arrived(rest, pixmap)
            return
        if self.dialog is None:
            return
        if kind == "details":
            self.dialog.details_arrived(result)
        elif kind == "you":
            found, matched = result
            self.matched.update(matched)
            self.dialog.set_cards("you", [Card(p.movie.title, p.movie.year, p.movie, None, p.because) for p in found])
            self._fill_mine()
            self.dialog.set_note("" if found else "TMDB had no recommendations for your films (yet): try Trending.")
        elif kind in ("trending", "search"):
            cards = [Card(m.title, m.year, m) for m in result]
            self.dialog.set_cards(kind, cards)
            if kind == "trending" and not self.cards_for("you"):
                self.dialog.set_cards("you", list(cards))
            self.dialog.set_note("" if cards or kind == "trending" else "Nothing found on TMDB.")
        self.dialog.refresh_buttons()

    def cards_for(self, tab: str) -> List[Card]:
        return self.dialog.cards.get(tab, []) if self.dialog is not None else []

    def _failed(self, tag: str, kind: str) -> None:
        name = tag.partition(":")[0]
        if name == "poster":
            return
        if name == "details":
            self._asked_details.discard(int(tag.partition(":")[2] or 0))
        self._loaded.pop("you" if name == "you" else name, None)
        if kind == "relay":                    # the relay can't ask TMDB: don't ask it again for a while
            self._relay_refused = time.monotonic()
            self._configure()
        if self.dialog is not None:
            problem = errors.problem(PROBLEMS.get(kind, "SW-635"))
            note = {"nokey": KEY_HELP, "relay": relay_help()}.get(kind, f"{problem.title}. {problem.explanation}")
            self.dialog.set_note(note)

    def _progress(self, tag: str, done: int, total: int) -> None:
        if self.dialog is not None and tag == "you" and total:
            self.dialog.set_note(f"Looking at your films… ({min(done, total)} of {total})")

    def _suggest(self, title: str, year: int, movie_id: int, poster: str) -> None:
        self.window.controller.votes.suggest(title, year, movie_id, poster)

    def _play(self, path: Path) -> None:
        if self.dialog is not None:
            self.dialog.hide()
        self.window.open_film(path)

    # ------------------------------------------------------------ the vote
    def vote_changed(self) -> None:
        c, panel = self.window.controller, self.window.sidebar.vote
        votes = c.votes
        vote = votes.vote
        panel.update_vote(vote.open, vote.choices, vote.winner, votes.my_vote, c.name, c.is_host,
                          self.have_keys(vote.choices) if vote.active else [])
        panel.setVisible(c.connected and vote.active)
        won = vote.find(vote.winner) if not vote.open else None
        if won is not None and not c.is_host and hasattr(self.window, "torrents") \
                and self.have_film(won.title, won.year, won.tmdb) is None:
            host = next((peer.name for peer in c.peers.values() if peer.is_host), "")
            if host and host in won.have:     # the host has it: room downloads (0.5) bring it here
                panel.status.setText(f"{panel.status.text()} When {host} plays it, you can get it straight from "
                                     f"{host}: it plays while it downloads.")
        if self.dialog is not None:
            self.dialog.refresh_buttons()

    def vote_won(self, choice: Choice) -> None:
        """(The activity line already says who won; a second note only if I can play it right away.)"""
        if self.have_film(choice.title, choice.year, choice.tmdb) is not None:
            self.window.overlay.toast(f"🏆 {choice.label} is in your library: ▶ Play it in the sidebar", "friend")

    def play_winner(self, key: int) -> None:
        choice = self.window.controller.votes.vote.find(key)
        path = self.have_film(choice.title, choice.year, choice.tmdb) if choice else None
        if path is not None:
            self.window.open_film(path)

    def where_winner(self, key: int) -> None:
        choice = self.window.controller.votes.vote.find(key)
        if choice is not None and choice.tmdb:
            open_link(watch_link(choice.tmdb))
