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
| `tools/` | Firewall helpers, "Test with two windows", and the release tools |
| `tests/` | Automatic tests (`tests/media` has a small demo film) |
| `docs/` | `GIT_GUIDE.md` (the Git lessons), `RELEASING.md` (publishing, signing key) |
| `packaging/` | Files that go into the friends' zip (README, licence notices) |
| `runtime-spec/` | Exactly which Python and libraries the portable runtime contains (with fingerprints) |
| `.github/workflows/` | The automatic tests GitHub runs for every change |
| `%APPDATA%\SyncWatch\` | **Your** settings, picture, logs, and the signing key. Never in this folder, never uploaded |

## Code map
| File | Job |
|---|---|
| `app.py` | Startup in stages: logging, crash catcher, one-window lock, settings, checks, window, restart |
| `errors.py` | Every SW error code with its plain-language text |
| `settings.py` | All settings in one table; the settings window builds itself from it |
| `sync/controller.py` | The rules of watching together (no Qt; tested with fake players) |
| `net/link.py`, `net/protocol.py` | The connection and strict checking of every message |
| `subtitles/` | Reading .srt/.vtt/.ass and tracks inside MKV files (no Qt) |
| `update/` | Finding, verifying (signatures), downloading and staging updates (no Qt) |
| `ui/` | Everything you see: main window, player, overlay, dialogs |

Everything outside `ui/` and `app.py` works without Qt, which keeps the logic testable and reusable.

## Tests
```
python -m unittest discover -s tests -p "test_*.py"     # logic, network, updates, release tool
python tests/gui_checks.py all                          # the real app, offscreen: window, two apps, update cycle
```
GitHub runs both on Windows and Linux for every pull request, and also builds the portable
runtime and runs the real app with it.

Security notes: [SECURITY.md](SECURITY.md).
