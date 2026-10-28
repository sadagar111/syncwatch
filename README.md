# SyncWatch

Watch local films in sync with friends: each of you plays your own copy, and
SyncWatch keeps play, pause and jumps together.

**Friends:** download `SyncWatch-<version>-windows.zip` from the Releases page, unzip,
double-click `SyncWatch.bat`. Nothing else to install; updates arrive by themselves.

**Developing (this folder):** double-click `SyncWatch.bat`. In a source-code folder it uses
the Python installed on your PC and installs the two libraries it needs once
(`requirements.txt`). Update it with `git pull`.

## Folders
| Folder / file | What it is |
|---|---|
| `syncwatch/` | The app's code (see below) |
| `assets/` | Design files: logo, background, icons, colours. See `assets/README.txt` |
| `boot.py` | Starter for the installed version: swaps in updates, goes back if one fails |
| `SyncWatch.bat` | Starts SyncWatch (installed version: its own Python; source folder: your Python) |
| `tools/` | "Test with two windows", fake viewers for big rooms (`bots.bat`), the release tools, a relay for testing (`local_relay.py`) |
| `relay/` | The relay server rooms go through (runs on Cloudflare; see `relay/README.md`) |
| `tests/` | Automatic tests (`tests/media` has a small demo film) |
| `docs/` | `GIT_GUIDE.md` (the Git lessons), `RELEASING.md` (publishing, signing key), `ROOMS.md` (how rooms work and why they're safe) |
| `packaging/` | Files that go into the friends' zip (README, licence notices) |
| `runtime-spec/` | Exactly which Python and libraries the portable runtime contains (with fingerprints) |
| `.github/workflows/` | The automatic tests GitHub runs for every change, and the relay's deploy |
| `%APPDATA%\SyncWatch\` | **Your** settings, picture, logs, and the signing key. Never in this folder, never uploaded |

## Code map
| File | Job |
|---|---|
| `app.py` | Startup in stages: logging, crash catcher, one-window lock, settings, checks, window, restart |
| `errors.py` | Every SW error code with its plain-language text |
| `settings.py` | All settings in one table; the settings window builds itself from it |
| `sync/controller.py` | The rules of watching together: who may do what, Sync up, gaps (no Qt; tested with fake players) |
| `room/session.py` | Hosting and joining a room: asking to join, letting in, room keys, who's in |
| `room/friends.py`, `room/store.py` | The friend list and friends' rooms; what's remembered (friends, blocked, your room's code) |
| `room/identity.py`, `codes.py`, `crypto.py` | Your keys, room codes, encryption and signatures of every message |
| `net/websocket.py`, `net/relay.py` | Connection to the relay server (reconnects by itself) |
| `net/protocol.py` | Strict checking of every message (allowed fields, sizes, ranges) |
| `subtitles/` | Reading .srt/.vtt/.ass and tracks inside MKV files (no Qt) |
| `update/` | Finding, verifying (signatures), downloading and staging updates (no Qt) |
| `ui/` | Everything you see: main window, player, overlay, dialogs |

Everything outside `ui/` and `app.py` works without Qt, which keeps the logic testable and reusable.

## Tests
```
python -m unittest discover -s tests -p "test_*.py"     # logic, rooms (up to 10 people), relay, updates, release tool
python tests/gui_checks.py all                          # the real app, offscreen: window, a room of three apps, update cycle
```
GitHub runs both on Windows and Linux for every pull request, and also builds the portable
runtime and runs the real app with it.

Security notes: [SECURITY.md](SECURITY.md).
