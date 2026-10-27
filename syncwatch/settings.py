"""All settings in one table, plus loading and saving them safely.

To add a setting, add one ``Setting(...)`` line to ``SCHEMA``. The settings
window builds itself from this table, so nothing else is needed for it to
appear (basic ones always; ``advanced=True`` ones only when
"Show advanced options" is ticked; ``hidden=True`` ones never: they're
remembered values such as the window size).

The file is written atomically (to a temporary file that then replaces the
old one), so a crash or power cut can't leave a half-written settings file.
A damaged file is kept as ``settings.json.damaged`` and defaults are used.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("syncwatch.settings")

FILE_VERSION = 1

SECTIONS = ["Profile", "Playback", "Subtitles", "Watching together", "Interface",
            "Keyboard", "Diagnostics"]


@dataclass(frozen=True)
class Setting:
    key: str
    kind: str              # bool | int | float | choice | str | color | path | font | keys
    default: Any
    label: str
    section: str
    help: str = ""
    advanced: bool = False
    hidden: bool = False
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    decimals: int = 1
    suffix: str = ""
    choices: Optional[Sequence[Tuple[str, str]]] = None  # (stored value, shown text)
    max_len: int = 300


def _s(*args, **kwargs) -> Setting:
    return Setting(*args, **kwargs)


P, PB, SB, WT, UI, KB, DG = SECTIONS

SCHEMA: List[Setting] = [
    # ---- Profile ----------------------------------------------------------------------
    _s("profile.name", "str", "", "Your name", P,
       "Shown to friends when you watch together.", max_len=32),

    # ---- Playback ---------------------------------------------------------------------
    _s("paths.movies_dir", "path", "", "Films folder", PB,
       "Where SyncWatch looks for films. Empty means the 'movies' folder next to the app."),
    _s("playback.resume", "bool", True, "Continue where I stopped", PB,
       "When you open a film again, jump to where you were."),
    _s("playback.resume_min_s", "int", 15, "Only continue if I watched at least", PB,
       "Don't bother resuming if you only watched the first few seconds.",
       advanced=True, minimum=0, maximum=600, suffix=" s"),
    _s("playback.skip_s", "int", 10, "Skip step", PB, "How far the skip buttons and arrow keys jump.",
       advanced=True, minimum=1, maximum=300, suffix=" s"),
    _s("playback.big_skip_s", "int", 60, "Big skip step", PB, "How far Shift + arrow keys jump.",
       advanced=True, minimum=5, maximum=1800, suffix=" s"),
    _s("playback.volume_step", "int", 5, "Volume step", PB, "How much the up/down keys change the volume.",
       advanced=True, minimum=1, maximum=25, suffix=" %"),
    _s("playback.wheel_volume", "bool", True, "Mouse wheel over the video changes volume", PB,
       advanced=True),
    _s("playback.audio_languages", "str", "sk,cs,en", "Preferred audio languages", PB,
       "Language codes separated by commas, best first, e.g. sk,cs,en. Used to pick the audio "
       "track automatically when a film has several.", advanced=True, max_len=60),
    _s("playback.volume", "int", 80, "Volume", PB, hidden=True, minimum=0, maximum=100),
    _s("playback.muted", "bool", False, "Muted", PB, hidden=True),

    # ---- Subtitles --------------------------------------------------------------------
    _s("subs.enabled", "bool", True, "Show subtitles", SB),
    _s("subs.auto_load", "bool", True, "Load subtitles automatically", SB,
       "Uses a subtitle file with the same name as the film, or a subtitle track inside the "
       "film, in your preferred language."),
    _s("subs.languages", "str", "sk,cs,en", "Preferred subtitle languages", SB,
       "Language codes separated by commas, best first, e.g. sk,cs,en.", max_len=60),
    _s("subs.size_pct", "float", 5.0, "Text size", SB, "Height of the text, as a share of the video height.",
       minimum=2.0, maximum=15.0, step=0.5, decimals=1, suffix=" %"),
    _s("subs.color", "color", "#FFFFFF", "Text colour", SB),
    _s("subs.box", "bool", False, "Dark box behind the text", SB,
       "Easier to read on bright scenes."),
    _s("subs.box_opacity", "int", 55, "Box darkness", SB, advanced=True, minimum=0, maximum=100,
       suffix=" %"),
    _s("subs.outline_px", "int", 3, "Outline thickness", SB, advanced=True, minimum=0, maximum=10,
       suffix=" px"),
    _s("subs.outline_color", "color", "#000000", "Outline colour", SB, advanced=True),
    _s("subs.font", "font", "", "Font", SB, "Empty means the default font.", advanced=True),
    _s("subs.bold", "bool", False, "Bold text", SB, advanced=True),
    _s("subs.margin_pct", "float", 6.0, "Distance from the bottom", SB,
       "Space between the subtitles and the bottom of the video.", advanced=True,
       minimum=0.0, maximum=40.0, step=0.5, decimals=1, suffix=" %"),
    _s("subs.encoding", "choice", "auto", "Character encoding of subtitle files", SB,
       "Only change this if letters like č, ř, ľ show up wrong.", advanced=True,
       choices=[("auto", "Detect automatically"), ("utf-8", "UTF-8"),
                ("cp1250", "Central European (Windows-1250)"), ("iso-8859-2", "Central European (ISO-8859-2)"),
                ("cp1252", "Western (Windows-1252)"), ("utf-16", "UTF-16")]),
    _s("subs.delay_step_ms", "int", 100, "Delay step for the Z / X keys", SB, advanced=True,
       minimum=10, maximum=5000, suffix=" ms"),
    _s("subs.mkv_read_limit_mb", "int", 0, "Read speed limit when reading subtitles from a film", SB,
       "Reading subtitle tracks scans the film file once (then they're cached). On an old hard "
       "drive this can make playback stutter for a minute; a limit avoids that. 0 = no limit.",
       advanced=True, minimum=0, maximum=5000, suffix=" MB/s"),

    # ---- Watching together ------------------------------------------------------------
    _s("sync.show_gap", "bool", True, "Show how far apart you are", WT,
       "Shows how many seconds your film is ahead of or behind the others."),
    _s("sync.show_ping", "bool", True, "Show ping", WT,
       "How long messages take between your PCs. This isn't the same as the gap."),
    _s("sync.gap_reference", "choice", "median", "Compare my position with", WT,
       "Median: the middle person among the others, so one person stuck loading doesn't skew it. "
       "With two people all three options mean the same thing.", advanced=True,
       choices=[("median", "The others' median"), ("average", "The others' average"),
                ("host", "The host")]),
    _s("sync.gap_refresh_s", "int", 10, "Update the gap every", WT, advanced=True,
       minimum=1, maximum=60, suffix=" s"),
    _s("sync.gap_warn_s", "float", 1.0, "Highlight Sync up when apart by more than", WT,
       advanced=True, minimum=0.2, maximum=10.0, step=0.1, decimals=1, suffix=" s"),
    _s("sync.countdown_s", "int", 3, "Countdown before resuming after Sync up", WT,
       "0 = resume immediately.", advanced=True, minimum=0, maximum=10, suffix=" s"),
    _s("sync.ready_timeout_s", "float", 5.0, "Wait for slow PCs at most", WT,
       "During Sync up, how long to wait for a friend whose film isn't ready yet before "
       "starting without them.", advanced=True, minimum=1.0, maximum=60.0, step=0.5, decimals=1,
       suffix=" s"),
    _s("sync.cooldown_playpause_s", "float", 2.0, "Friends' play/pause cooldown", WT,
       "When you host: how often a friend may pause or play. You (the host) have no cooldown.",
       advanced=True, minimum=0.0, maximum=30.0, step=0.5, decimals=1, suffix=" s"),
    _s("sync.cooldown_syncup_s", "float", 15.0, "Friends' Sync up cooldown", WT,
       "When you host: how often a friend may use Sync up.", advanced=True,
       minimum=0.0, maximum=300.0, step=1.0, decimals=0, suffix=" s"),
    _s("sync.seek_debounce_ms", "int", 400, "Send skips after you stop pressing for", WT,
       "Pressing skip many times quickly sends one jump at the end instead of many.",
       advanced=True, minimum=0, maximum=3000, suffix=" ms"),
    _s("sync.host_only", "bool", False, "Only I can control playback when I host", WT,
       "Friends' buttons then work just for themselves."),
    _s("sync.compensate_latency", "bool", True, "Compensate for network delay", WT, advanced=True),
    _s("sync.port", "int", 50505, "Port", WT, "Both of you must use the same port.",
       advanced=True, minimum=1024, maximum=65535),
    _s("sync.last_address", "str", "", "Last address", WT, hidden=True, max_len=120),

    # ---- Interface --------------------------------------------------------------------
    _s("ui.show_advanced", "bool", False, "Show advanced options", UI),
    _s("ui.settings_help_inline", "bool", False, "Show explanations under the settings", UI,
       "Writes each setting's explanation underneath it, instead of only when you point at the ⓘ."),
    _s("ui.sidebar", "bool", True, "Show the sidebar", UI),
    _s("ui.panel_connection", "bool", True, "Show the connection panel", UI),
    _s("ui.panel_people", "bool", True, "Show the people panel", UI),
    _s("ui.panel_activity", "bool", True, "Show the activity feed", UI),
    _s("ui.markers", "bool", True, "Show friends on the seek bar", UI),
    _s("ui.end_clock", "bool", True, "Show when the film ends", UI),
    _s("ui.toasts", "bool", True, "Show short messages over the video", UI),
    _s("ui.remaining", "bool", True, "Show time remaining", UI, advanced=True),
    _s("ui.hover_time", "bool", True, "Show the time when hovering over the seek bar", UI,
       advanced=True),
    _s("ui.center_flash", "bool", True, "Flash the play/pause symbol", UI, advanced=True),
    _s("ui.sidebar_in_fullscreen", "bool", False, "Keep the sidebar in fullscreen", UI, advanced=True),
    _s("ui.hide_delay_ms", "int", 2500, "Hide controls in fullscreen after", UI, advanced=True,
       minimum=500, maximum=15000, suffix=" ms"),
    _s("ui.toast_s", "int", 5, "Show messages for", UI, advanced=True, minimum=1, maximum=30, suffix=" s"),
    _s("ui.activity_max", "int", 200, "Activity feed length", UI, advanced=True,
       minimum=20, maximum=2000, suffix=" entries"),
    _s("ui.startup_checks", "choice", "problems", "Show the startup checks", UI, advanced=True,
       choices=[("problems", "Only when there's a problem"), ("always", "Always"), ("never", "Never")]),
    _s("ui.video_selftest", "bool", True, "Test video playback at startup", UI,
       "Plays a tiny test video for about a second to make sure films will play.", advanced=True),
    _s("ui.window_geometry", "str", "", "Window position", UI, hidden=True, max_len=4000),
    _s("ui.splitter", "str", "", "Sidebar width", UI, hidden=True, max_len=4000),

    # ---- Keyboard ---------------------------------------------------------------------
    _s("keys.play_pause", "keys", "Space", "Play / pause", KB, advanced=True),
    _s("keys.back", "keys", "Left", "Skip back", KB, advanced=True),
    _s("keys.forward", "keys", "Right", "Skip forward", KB, advanced=True),
    _s("keys.back_big", "keys", "Shift+Left", "Big skip back", KB, advanced=True),
    _s("keys.forward_big", "keys", "Shift+Right", "Big skip forward", KB, advanced=True),
    _s("keys.volume_up", "keys", "Up", "Volume up", KB, advanced=True),
    _s("keys.volume_down", "keys", "Down", "Volume down", KB, advanced=True),
    _s("keys.mute", "keys", "M", "Mute", KB, advanced=True),
    _s("keys.fullscreen", "keys", "F", "Fullscreen", KB, advanced=True),
    _s("keys.sync_me", "keys", "S", "Sync me (jump only myself)", KB, advanced=True),
    _s("keys.sync_up", "keys", "Ctrl+S", "Sync up (everyone)", KB, advanced=True),
    _s("keys.subs_toggle", "keys", "C", "Subtitles on/off", KB, advanced=True),
    _s("keys.subs_earlier", "keys", "Z", "Subtitles earlier", KB, advanced=True),
    _s("keys.subs_later", "keys", "X", "Subtitles later", KB, advanced=True),
    _s("keys.sidebar", "keys", "Tab", "Show/hide sidebar", KB, advanced=True),
    _s("keys.open", "keys", "Ctrl+O", "Open film", KB, advanced=True),
    _s("keys.settings", "keys", "Ctrl+,", "Settings", KB, advanced=True),
    _s("keys.help", "keys", "F1", "Keyboard shortcuts", KB, advanced=True),

    # ---- Diagnostics ------------------------------------------------------------------
    _s("log.debug", "bool", False, "Detailed logging", DG,
       "Writes much more to the log file. Useful when hunting a problem.", advanced=True),
    _s("log.keep", "int", 5, "Log files to keep", DG, advanced=True, minimum=1, maximum=50),
]

BY_KEY: Dict[str, Setting] = {s.key: s for s in SCHEMA}
if len(BY_KEY) != len(SCHEMA):  # pragma: no cover - caught by tests
    raise RuntimeError("duplicate setting key in SCHEMA")

_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")


def coerce(setting: Setting, value: Any) -> Tuple[bool, Any]:
    """Return (ok, cleaned value). Numbers are clamped into range."""
    kind = setting.kind
    try:
        if kind == "bool":
            if isinstance(value, bool):
                return True, value
            if value in (0, 1):
                return True, bool(value)
            return False, setting.default
        if kind in ("int", "float"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, setting.default
            number = float(value)
            if number != number:  # NaN
                return False, setting.default
            if setting.minimum is not None:
                number = max(setting.minimum, number)
            if setting.maximum is not None:
                number = min(setting.maximum, number)
            return True, int(round(number)) if kind == "int" else number
        if kind == "choice":
            allowed = [c[0] for c in (setting.choices or [])]
            return (True, value) if value in allowed else (False, setting.default)
        if kind == "color":
            return (True, value.upper()) if isinstance(value, str) and _COLOR.match(value) else (False, setting.default)
        if kind in ("str", "path", "font", "keys"):
            if not isinstance(value, str):
                return False, setting.default
            value = value.replace("\x00", "")
            if kind != "path":
                value = value.strip()
            return True, value[: setting.max_len]
    except Exception:  # pragma: no cover - defensive
        pass
    return False, setting.default


Listener = Callable[[str, Any], None]


class Settings:
    """Current setting values. Thread-safe; listeners are told about every change."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        self._values: Dict[str, Any] = {s.key: s.default for s in SCHEMA}
        self._unknown: Dict[str, Any] = {}  # keys from newer versions, kept as they are
        self._listeners: List[Tuple[str, Listener]] = []
        self._lock = threading.RLock()
        self._dirty = False
        self.writable = path is not None

    # ---- loading / saving ----------------------------------------------------------
    def load(self) -> List[str]:
        """Read the file. Returns problem codes to report (e.g. ["SW-106"])."""
        problems: List[str] = []
        if self.path is None or not self.path.exists():
            return problems
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("values", {}), dict):
                raise ValueError("unexpected file structure")
        except (OSError, ValueError) as exc:
            log.warning("settings file damaged (%s); using defaults", exc)
            self._keep_damaged_copy()
            return ["SW-106"]
        values = raw.get("values", {})
        with self._lock:
            for key, value in values.items():
                setting = BY_KEY.get(key)
                if setting is None:
                    self._unknown[key] = value
                    continue
                ok, cleaned = coerce(setting, value)
                if not ok:
                    log.info("setting %s had invalid value %r; using default", key, value)
                self._values[key] = cleaned
        return problems

    def _keep_damaged_copy(self) -> None:
        try:
            damaged = self.path.with_name(self.path.name + ".damaged")
            os.replace(self.path, damaged)
        except OSError:
            pass

    def save(self) -> bool:
        """Write the file now (atomically). Returns False if it couldn't be written."""
        if self.path is None:
            return False
        with self._lock:
            data = {"_version": FILE_VERSION,
                    "values": {**self._unknown, **{k: v for k, v in self._values.items()}}}
            self._dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            self.writable = True
            return True
        except OSError as exc:
            log.warning("could not save settings: %s", exc)
            self.writable = False
            return False

    def save_if_dirty(self) -> None:
        """Called on a timer, so a slider being dragged doesn't write the file 100 times."""
        if self._dirty:
            self.save()

    # ---- reading / changing ---------------------------------------------------------
    def get(self, key: str) -> Any:
        with self._lock:
            return self._values[key]

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def set(self, key: str, value: Any) -> bool:
        """Change a value. Returns True if it actually changed."""
        setting = BY_KEY[key]
        ok, cleaned = coerce(setting, value)
        if not ok:
            log.warning("rejected invalid value %r for %s", value, key)
            return False
        with self._lock:
            if self._values.get(key) == cleaned:
                return False
            self._values[key] = cleaned
            self._dirty = True
            listeners = list(self._listeners)
        for prefix, listener in listeners:
            if key.startswith(prefix):
                try:
                    listener(key, cleaned)
                except Exception:  # a broken listener must not stop the others
                    log.exception("settings listener failed for %s", key)
        return True

    def reset(self, key: Optional[str] = None) -> None:
        keys = [key] if key else [s.key for s in SCHEMA if not s.hidden]
        for k in keys:
            self.set(k, BY_KEY[k].default)

    def is_default(self, key: str) -> bool:
        return self.get(key) == BY_KEY[key].default

    def changed_advanced(self) -> List[str]:
        """Advanced settings that differ from their defaults (shown as a hint when hidden)."""
        return [s.key for s in SCHEMA if s.advanced and not s.hidden and not self.is_default(s.key)]

    def subscribe(self, prefix: str, listener: Listener) -> None:
        """Call ``listener(key, value)`` whenever a key starting with ``prefix`` changes."""
        with self._lock:
            self._listeners.append((prefix, listener))

    def languages(self, key: str) -> List[str]:
        """Parse a comma-separated language preference into lowercase codes."""
        return [part.strip().lower() for part in str(self.get(key)).split(",") if part.strip()]
