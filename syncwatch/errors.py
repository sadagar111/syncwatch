"""Every problem SyncWatch can report, written for people who aren't technical.

Each problem has a code like ``SW-402``. The popup shows the plain-language
title, explanation and tips first; the code and technical details come
underneath, with a "Copy details" button, so friends can send you the code
instead of you having to explain every time what went wrong.

Code ranges:
    1xx  installation, files, settings
    2xx  video, sound, the film library (22x), subtitles (25x)
    3xx  the network
    4xx  rooms and the relay server
    5xx  watching in sync
    6xx  film sources such as Real-Debrid / Stremio (later)
    7xx  updates
    8xx  profile pictures
    9xx  unexpected errors and crashes

To report a problem anywhere in the code::

    from syncwatch import errors
    errors.report("SW-407", technical=f"{type(e).__name__}: {e}", reason="it is too short")

Extra keyword arguments fill ``{placeholders}`` in the texts. The UI registers
a handler that shows the popup; without one (tests, early startup) problems are
only logged.
"""
from __future__ import annotations

import logging
import platform
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import __version__

log = logging.getLogger("syncwatch.errors")

ERROR, WARNING, INFO = "error", "warning", "info"


@dataclass(frozen=True)
class Problem:
    title: str
    explanation: str
    tips: List[str] = field(default_factory=list)
    severity: str = ERROR
    popup: bool = True  # False = only log it (and show a short toast if the UI wants)


CATALOG: Dict[str, Problem] = {
    # ---- 1xx: installation, files, settings ---------------------------------
    "SW-101": Problem(
        "Python is too old",
        "SyncWatch needs Python 3.9 or newer, and this PC has an older version.",
        ["Install Python 3.13 from python.org and tick \"Add python.exe to PATH\" during setup.",
         "Then start SyncWatch again."]),
    "SW-102": Problem(
        "A part SyncWatch needs is missing",
        "The video and window library (PySide6) isn't installed.",
        ["Start SyncWatch with SyncWatch.bat. It installs the library automatically.",
         "If that fails, check your internet connection and try again."]),
    "SW-103": Problem(
        "A part SyncWatch needs is too old or damaged",
        "The video and window library (PySide6) is older than version 6.5 or damaged.",
        ["Open a command prompt and run:  python -m pip install --upgrade PySide6",
         "Then start SyncWatch again."]),
    "SW-104": Problem(
        "Couldn't install what SyncWatch needs",
        "Downloading the video and window library failed.",
        ["Check your internet connection.",
         "If your Python is brand new (3.15 or newer), install Python 3.13 instead. The library "
         "isn't available for brand-new Python versions straight away.",
         "Antivirus or a school/work network can block downloads. Try another network."]),
    "SW-105": Problem(
        "SyncWatch can't save its settings",
        "Windows didn't allow SyncWatch to write to its settings folder, so your settings and "
        "film positions won't be saved this time.",
        ["If your antivirus protects folders (e.g. Windows \"Controlled folder access\"), allow Python.",
         "Make sure the disk isn't full."], WARNING),
    "SW-106": Problem(
        "Your settings were reset",
        "The settings file was damaged, so SyncWatch started with default settings. "
        "A copy of the damaged file was kept next to it.",
        ["Nothing to do. Set your preferences again in Settings if needed."], WARNING),
    "SW-107": Problem(
        "Saved film positions were reset",
        "The file that remembers where you stopped in each film was damaged and was started fresh.",
        [], INFO, popup=False),
    "SW-108": Problem(
        "SyncWatch is already open",
        "SyncWatch is already running with this profile. Two copies with the same profile would "
        "overwrite each other's settings.",
        ["Switch to the SyncWatch window that's already open.",
         "To test with two windows on one PC, use \"Test with two windows.bat\" in the tools folder."]),
    "SW-109": Problem(
        "That profile name can't be used",
        "Profile names can only contain letters, numbers, - and _ (up to 32 characters).",
        ["Example:  SyncWatch.bat --profile Test2"]),
    "SW-111": Problem(
        "Some design images are missing",
        "One or more images in the assets folder are missing or damaged, so SyncWatch uses "
        "plain defaults for them.",
        ["If you replaced an image, make sure it's a real PNG file with exactly the same name."],
        WARNING, popup=False),
    "SW-112": Problem(
        "The theme file has a mistake",
        "assets/theme.json couldn't be read, so the default colours are used.",
        ["Check the file for a missing comma or quote, or put back the original file."], WARNING),

    # ---- 2xx: video and sound ------------------------------------------------
    "SW-201": Problem(
        "Video playback isn't available",
        "SyncWatch couldn't load its video components.",
        ["Reinstall the library:  python -m pip install --force-reinstall PySide6",
         "(The full PySide6 package is needed. PySide6-Essentials alone has no video part.)",
         "Restart the PC and try again."]),
    "SW-202": Problem(
        "No video formats are available",
        "The video components loaded, but they report that they can't play any format. "
        "They're probably damaged.",
        ["Reinstall the library:  python -m pip install --force-reinstall PySide6"]),
    "SW-203": Problem(
        "The video test didn't pass",
        "At startup SyncWatch plays a tiny test video, and it didn't work. Films may not play.",
        ["Update your graphics driver.",
         "If you're connected through Remote Desktop, try directly on the PC.",
         "Try opening a film anyway. If it plays fine, you can turn this test off in Settings "
         "(advanced, Interface)."], WARNING),
    "SW-204": Problem(
        "Film not found",
        "The film file doesn't exist anymore. It was probably moved, renamed or deleted.",
        ["Open it again from its new location."]),
    "SW-205": Problem(
        "This film can't be played",
        "SyncWatch couldn't open this file. It may be damaged, not fully downloaded, or in a "
        "format SyncWatch doesn't support.",
        ["If it's still downloading, wait until it's finished.",
         "Try another copy of the film (a different release).",
         ".mkv, .mp4, .avi, .mov and .webm files usually work."]),
    "SW-206": Problem(
        "Playback stopped because of an error",
        "Something went wrong while playing the film.",
        ["Try pressing play again.", "If it keeps happening at the same spot, the file may be damaged."]),
    "SW-207": Problem(
        "The film file is in use",
        "Another program, often a download app or antivirus, is using this file right now.",
        ["Wait until the download has finished.", "Close other programs that might be using it."]),
    "SW-210": Problem(
        "No sound device found",
        "Windows reports no speakers or headphones, so films will play without sound.",
        ["Plug in headphones or speakers, then restart SyncWatch.",
         "Check that a sound device is enabled in Windows sound settings."], WARNING),
    "SW-208": Problem(
        "The picture can't be shown",
        "The video player couldn't draw the picture on this PC, so you may only hear the sound.",
        ["Settings > Playback > How mpv draws the picture (advanced): pick the other option, then "
         "restart SyncWatch.",
         "Update your graphics driver.",
         "If nothing helps, set Video player (advanced, same page) to Qt."], WARNING),
    "SW-209": Problem(
        "SyncWatch uses its previous video player",
        "The video player (mpv) couldn't start on this PC, so SyncWatch plays films with the player "
        "of version 0.4.1. Films play as before, but a few sound formats may not work.",
        ["If you run SyncWatch from its source code, start it with SyncWatch.bat once: it fetches mpv.",
         "Otherwise click \"Copy details\" and send them to whoever gave you SyncWatch."],
        WARNING, popup=False),
    "SW-211": Problem(
        "Sound moved to another device",
        "Your speakers or headphones were disconnected, so sound now plays on the Windows "
        "default device.",
        [], INFO, popup=False),
    "SW-212": Problem(
        "This film's sound can't be played",
        "The picture plays, but the video player can't decode this film's sound.",
        ["If the film has several audio tracks, try another one (Subtitles button > Audio track).",
         "Add the film with Library > Add film: SyncWatch then offers to convert the sound "
         "(the picture isn't touched)."], WARNING),

    # ---- 22x: the film library -----------------------------------------------
    "SW-220": Problem(
        "Not enough space for this film",
        "The drive with your library folder has {free} free, and this needs {needed}.",
        ["Free up some space and try again.",
         "Or choose a library folder on a bigger drive: Settings > Playback > Library folder."]),
    "SW-221": Problem(
        "Couldn't copy the film into the library",
        "Copying stopped because of an error. Nothing was added, and the original file is untouched.",
        ["Check that the drive isn't full or disconnected, then try again.",
         "Antivirus can block copying. Allow SyncWatch in it."]),
    "SW-222": Problem(
        "Couldn't convert the film",
        "The converter stopped because of an error. Nothing was added, and the original file is "
        "untouched.",
        ["Try another copy (release) of the film.",
         "If it keeps happening, click \"Copy details\" and send them to whoever gave you SyncWatch."]),
    "SW-223": Problem(
        "This film can't be played",
        "{reason}",
        ["If it's still downloading, wait until it's finished.",
         "Try another copy (release) of the film."]),
    "SW-224": Problem(
        "Couldn't remove the film",
        "Windows didn't allow SyncWatch to delete it. It may be open in another program.",
        ["Close other programs that might use it and try again, or delete it in File Explorer "
         "(Library > Show in folder)."], WARNING),
    "SW-225": Problem(
        "The converter is missing",
        "This copy of SyncWatch can't convert films, because its converter (PyAV) isn't there.",
        ["If you run SyncWatch from its source code, start it with SyncWatch.bat once: it installs "
         "what's missing.",
         "Otherwise download the full SyncWatch zip again and unzip it over this folder."]),

    # ---- 25x: subtitles ------------------------------------------------------
    "SW-250": Problem(
        "Can't read this subtitle file",
        "The file couldn't be opened, or it's too large to be a subtitle file (over {limit_mb} MB).",
        ["Check that the file isn't open in another program.", "Try downloading the subtitles again."]),
    "SW-251": Problem(
        "This doesn't look like a subtitle file",
        "SyncWatch reads .srt, .vtt, .ass and .ssa files, and this file had no subtitles it "
        "could understand.",
        ["Make sure you picked the subtitle file and not something else.",
         "Try downloading the subtitles again, ideally as .srt."]),
    "SW-252": Problem(
        "Couldn't read the subtitles inside this film",
        "Reading the subtitle track from the film failed, so subtitles may be missing for "
        "part of the film.",
        ["Use a separate .srt subtitle file instead."], WARNING),
    "SW-253": Problem(
        "This subtitle track can't be shown",
        "This track stores subtitles as pictures (common in Blu-ray copies). SyncWatch can "
        "only show text subtitles.",
        ["Download an .srt subtitle file for this film and put it next to the film with the "
         "same name, or load it with Subtitles > Load file."], INFO),

    # ---- 3xx: the network --------------------------------------------------
    # Numbers 301 to 310 and 312 belonged to the direct connection (up to 0.3.1).
    # They're retired: don't reuse them, old reports may still mention them.
    "SW-311": Problem(
        "No network connection",
        "This PC doesn't seem to be connected to a network.",
        ["Check your Wi-Fi or cable, then try again."]),

    # ---- 4xx: rooms and the relay server -------------------------------------
    "SW-401": Problem(
        "Rooms aren't set up yet",
        "This copy of SyncWatch doesn't know which relay server rooms go through.",
        ["Update SyncWatch (Settings > Updates > Check now).",
         "If you publish SyncWatch yourself: put the relay online and run tools\\release.bat "
         "set-relay (docs/GIT_GUIDE.md, lesson 5)."]),
    "SW-402": Problem(
        "Can't reach the relay server",
        "SyncWatch couldn't connect to the server rooms go through. It keeps trying by itself.",
        ["Check that this PC is online (open any website).",
         "A firewall or antivirus may be blocking SyncWatch. Allow SyncWatch (or pythonw.exe in "
         "its runtime folder) to use the internet."], WARNING, popup=False),
    "SW-403": Problem(
        "Secure connection to the relay failed",
        "The relay server's certificate couldn't be checked, so SyncWatch didn't connect. "
        "This protects you from someone pretending to be the relay.",
        ["Check that the date and time on this PC are correct.",
         "Some antivirus programs and school or work networks inspect secure connections. "
         "Try another network, or turn off \"HTTPS scanning\" in the antivirus."]),
    "SW-404": Problem(
        "Too many connections to this room",
        "The relay server allows a limited number of connections per room, and they're all in use.",
        ["Wait a moment; SyncWatch keeps trying.",
         "If it keeps happening, the host can press New code (someone may be abusing the old one)."],
        WARNING, popup=False),
    "SW-405": Problem(
        "The relay server answered strangely",
        "The address for the relay server may be wrong, or the relay is a different version.",
        ["Check Settings > Watching together > Relay server (advanced). Empty means the built-in one.",
         "Update SyncWatch (Settings > Updates > Check now)."]),
    "SW-406": Problem(
        "Slow down",
        "Too many messages were sent in a short time, so the relay server dropped some.",
        [], INFO, popup=False),
    "SW-407": Problem(
        "That room code doesn't work",
        "SyncWatch couldn't read it: {reason}.",
        ["Ask for the code again and paste it with Ctrl+V. The host's Copy code button copies it exactly."]),
    "SW-408": Problem(
        "Someone pretended to be the host",
        "A message in this room claimed to come from the host but wasn't signed with the host's key. "
        "SyncWatch ignored it.",
        ["If it keeps happening, ask the host to press New code and send you the new one."],
        WARNING, popup=False),
    "SW-409": Problem(
        "The host didn't let you in",
        "The host turned down your request to join.",
        ["Ask the host, then try again."], INFO),
    "SW-410": Problem(
        "The room is full",
        "The room already has as many people as the host allows.",
        ["The host can raise the limit in Settings > Watching together > Room size limit (advanced)."], INFO),
    "SW-411": Problem(
        "You were removed from the room",
        "The host removed you from the room.",
        [], INFO),
    "SW-412": Problem(
        "{host} closed the room",
        "Your film keeps playing on its own.",
        [], INFO, popup=False),
    "SW-413": Problem(
        "You have different SyncWatch versions",
        "This room runs a different version of SyncWatch than yours, and they can't work together.",
        ["Everyone needs the newest version: Settings > Updates > Check now."]),
    "SW-414": Problem(
        "Your SyncWatch identity was renewed",
        "The file with your keys was damaged, so SyncWatch made new ones. Friends will see you as a new "
        "person with a new ID.",
        ["Add your friends again (in a room, People list > Add friend).",
         "The damaged file is kept in your profile folder, in case you want to look at it."], WARNING),
    "SW-415": Problem(
        "Nobody is hosting this room right now",
        "SyncWatch keeps waiting and asks to join as soon as the host opens the room.",
        [], INFO, popup=False),
    "SW-416": Problem(
        "This room is for friends only",
        "The host only lets friends join.",
        ["Ask the host to allow everyone to ask (Settings > Watching together), or to add you as a "
         "friend the next time you're in a room together."], INFO),
    "SW-417": Problem(
        "The host is busy",
        "Too many people are waiting to be let in right now.",
        ["Try again in a minute."], INFO),
    "SW-419": Problem(
        "Your friend list was reset",
        "The file with your room code, friends and blocked people was damaged, so SyncWatch started fresh.",
        ["Your room has a new code: send it to your friends again.",
         "Add friends again when you're in a room together."], WARNING),

    # ---- 5xx: watching in sync ----------------------------------------------
    "SW-501": Problem(
        "You have a different film file",
        "{detail}",
        ["Make sure you both opened the same film.",
         "If it's the same film from a different release, small differences can be fixed "
         "later with a personal offset."], WARNING, popup=False),
    "SW-502": Problem(
        "Please wait a moment",
        "You just did that. Try again in {seconds} s.",
        [], INFO, popup=False),
    "SW-503": Problem(
        "{name} wasn't ready in time",
        "Playback started without them. They'll catch up when their film is ready.",
        [], INFO, popup=False),
    "SW-504": Problem(
        "The host has limited your controls",
        "Only the host can do that right now. Your buttons work just for you until the host "
        "allows everyone again.",
        [], INFO, popup=False),

    # ---- 7xx: updates -------------------------------------------------------
    "SW-701": Problem(
        "Couldn't check for updates",
        "SyncWatch couldn't reach GitHub to look for a new version. Everything else works normally.",
        ["Check your internet connection and try again later.",
         "Antivirus \"web protection\" or a school/work network can block this. Try another network."],
        INFO, popup=False),
    "SW-702": Problem(
        "GitHub is busy",
        "GitHub limits how often one internet connection can ask about updates. SyncWatch will try "
        "again later by itself.",
        [], INFO, popup=False),
    "SW-703": Problem(
        "An update was refused for your safety",
        "SyncWatch found a new version, but it isn't signed by the person who publishes SyncWatch, "
        "or it was damaged on the way. Nothing was downloaded or installed.",
        ["Keep using your current version. It's not affected.",
         "Click \"Copy details\" and send them to whoever gave you SyncWatch."]),
    "SW-704": Problem(
        "The update download didn't finish",
        "The connection broke off while downloading. Nothing was installed. Next time the download "
        "continues where it stopped.",
        ["Check your internet connection and click Update again."], WARNING),
    "SW-705": Problem(
        "The downloaded update was damaged",
        "The downloaded file doesn't match the published fingerprint, so it was thrown away. "
        "Nothing was installed.",
        ["Try again.",
         "If it keeps happening, antivirus or a network filter may be changing downloads. Try "
         "another network."], WARNING),
    "SW-706": Problem(
        "The update couldn't be installed",
        "SyncWatch couldn't put the new version in place, so you're still on the version you had.",
        ["Close all SyncWatch windows and start SyncWatch again. The update is retried automatically.",
         "If SyncWatch is in a protected folder (like Program Files), move the whole SyncWatch folder "
         "to Documents.",
         "Antivirus can block changing files. Allow SyncWatch's folder in it."]),
    "SW-707": Problem(
        "SyncWatch went back to the previous version",
        "Version {version} didn't start properly twice in a row, so SyncWatch went back to version "
        "{to}. Automatic updates will skip version {version}.",
        ["Click \"Copy details\" and send them to whoever gave you SyncWatch."], WARNING),
    "SW-709": Problem(
        "Updates are off in the source-code version",
        "You're running SyncWatch from its source code (for example a Git folder), so the update "
        "button is switched off.",
        ["Update with Git instead:  git pull"], INFO),
    "SW-710": Problem(
        "Not enough disk space for the update",
        "There isn't enough free space on the drive where SyncWatch is.",
        ["Free up some space (the details show how much is needed), then click Update again."]),
    "SW-711": Problem(
        "Updates aren't set up yet",
        "This copy of SyncWatch doesn't know where updates are published yet.",
        [], INFO, popup=False),
    "SW-712": Problem(
        "SyncWatch couldn't start",
        "The starter (boot.py) couldn't start the app.",
        ["Start SyncWatch again.",
         "Download the full SyncWatch zip again and unzip it over this folder. Your settings are kept, "
         "they're stored separately."]),
    "SW-713": Problem(
        "SyncWatch's own Python is missing or damaged",
        "The runtime folder, which holds the Python that SyncWatch brings along, is incomplete.",
        ["Download the full SyncWatch zip again and unzip it over this folder. Your settings are kept."]),
    "SW-714": Problem(
        "Went back to the previous version",
        "As you asked, SyncWatch went back from version {version} to version {to}.",
        [], INFO, popup=False),

    # ---- 8xx: profile pictures ----------------------------------------------
    "SW-801": Problem(
        "Can't open this picture",
        "The file isn't a picture SyncWatch can read (PNG, JPG, WebP, BMP or GIF), or it's damaged.",
        ["Try another picture, or save this one as PNG or JPG first."]),
    "SW-802": Problem(
        "This picture is too big",
        "Pictures can be at most {limit_mb} MB.",
        ["Pick a smaller picture, or make it smaller in another program first."]),
    "SW-803": Problem(
        "A friend's profile picture was ignored",
        "A picture received from a friend wasn't a valid small image, so it wasn't shown.",
        [], INFO, popup=False),

    # ---- 9xx: unexpected ----------------------------------------------------
    "SW-901": Problem(
        "Something went wrong inside SyncWatch",
        "An unexpected error happened. SyncWatch will try to keep running.",
        ["If it keeps happening, click \"Copy details\" and send them to whoever gave you SyncWatch."]),
    "SW-902": Problem(
        "Something went wrong in the background",
        "An unexpected error happened in a background task. SyncWatch will try to keep running.",
        ["If it keeps happening, click \"Copy details\" and send them to whoever gave you SyncWatch."]),
    "SW-903": Problem(
        "The video system reported a problem",
        "Qt, the library SyncWatch uses for video and windows, reported a critical problem.",
        [], WARNING, popup=False),
    "SW-990": Problem(
        "SyncWatch closed unexpectedly last time",
        "The details were saved.",
        ["If it happens again, click \"Copy details\" and send them to whoever gave you SyncWatch."],
        WARNING),
}

UNKNOWN_CODE = "SW-901"


class SyncWatchError(Exception):
    """An expected failure with a catalog code, raised by Qt-free modules."""

    def __init__(self, code: str, technical: str = "", **params):
        super().__init__(f"{code}: {technical}")
        self.code = code
        self.technical = technical
        self.params = params


@dataclass
class Report:
    """One reported problem, ready to show."""
    code: str
    problem: Problem
    title: str
    explanation: str
    tips: List[str]
    technical: str
    details: str  # everything "Copy details" puts on the clipboard
    when: float


Handler = Callable[[Report], None]
_handler: Optional[Handler] = None
_recent: Dict[str, float] = {}
_recent_lock = threading.Lock()
DEDUPE_SECONDS = 15.0


def set_handler(handler: Optional[Handler]) -> None:
    """The UI calls this with a function that shows the popup (must be thread-safe)."""
    global _handler
    _handler = handler


def problem(code: str) -> Problem:
    return CATALOG.get(code) or CATALOG[UNKNOWN_CODE]


def _fill(text: str, params: dict) -> str:
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        return text  # a missing placeholder must never break error reporting


def describe_exception(exc: BaseException) -> str:
    """Short one-line technical description, e.g. 'TimeoutError: timed out'."""
    text = str(exc).strip() or "(no message)"
    return f"{type(exc).__name__}: {text}"


def system_summary() -> str:
    qt = "not loaded"
    if "PySide6" in sys.modules:
        try:
            import PySide6  # noqa: WPS433
            from PySide6 import QtCore
            qt = f"PySide6 {PySide6.__version__}, Qt {QtCore.qVersion()}"
        except Exception:  # pragma: no cover - only if Qt is half-broken
            qt = "PySide6 (version unknown)"
    return (f"SyncWatch {__version__} | Python {platform.python_version()} | "
            f"{platform.system()} {platform.release()} ({platform.machine()}) | {qt}")


def build(code: str, technical: str = "", exc: Optional[BaseException] = None, **params) -> Report:
    prob = problem(code)
    if code not in CATALOG:
        technical = f"(unknown code {code}) {technical}".strip()
        code = UNKNOWN_CODE
    if exc is not None and not technical:
        technical = describe_exception(exc)
    title = _fill(prob.title, params)
    explanation = _fill(prob.explanation, params)
    tips = [_fill(t, params) for t in prob.tips]
    when = time.time()
    lines = [
        f"Error {code}: {title}",
        f"Technical: {technical or '-'}",
        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(when))}",
        f"System: {system_summary()}",
    ]
    if exc is not None and exc.__traceback__ is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        lines.append("Traceback (most recent call last, shortened):")
        lines.extend("  " + ln for ln in tb.strip().splitlines()[-14:])
    return Report(code, prob, title, explanation, tips, technical, "\n".join(lines), when)


def report(code: str, technical: str = "", exc: Optional[BaseException] = None,
           popup: Optional[bool] = None, **params) -> Report:
    """Log a problem and, unless it's log-only or a recent duplicate, show it.

    Never raises: a failure while reporting a problem must not cause a new one.
    """
    try:
        rep = build(code, technical, exc, **params)
    except Exception as build_error:  # pragma: no cover - defensive
        log.exception("error while building report for %s: %s", code, build_error)
        return build(UNKNOWN_CODE, f"report failed for {code}")
    level = {ERROR: logging.ERROR, WARNING: logging.WARNING}.get(rep.problem.severity, logging.INFO)
    log.log(level, "%s %s | %s", rep.code, rep.title, rep.technical or "-",
             exc_info=(type(exc), exc, exc.__traceback__) if exc is not None else None)
    show = rep.problem.popup if popup is None else popup
    if show and _handler is not None:
        key = f"{rep.code}|{rep.technical}"
        now = time.monotonic()
        with _recent_lock:
            if now - _recent.get(key, -1e9) < DEDUPE_SECONDS:
                return rep
            _recent[key] = now
        try:
            _handler(rep)
        except Exception:  # pragma: no cover - the UI handler itself failed
            log.exception("error popup handler failed")
    return rep
