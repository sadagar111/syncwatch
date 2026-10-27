# Changelog

## 0.3.0 (step 2: updates and easy install)
**Updates with one click**
- SyncWatch checks GitHub for new versions when it starts: at most every 6 hours, adjustable, and never more often than needed.
- A banner shows the new version. **What's new** lists the changes, **Update now** downloads with progress, speed and time left, and **Restart** finishes it. "Skip this version" and "Later" are there too.
- Every update is **signed by the publisher**. Anything unsigned, wrongly signed, damaged or older is refused (SW-703 / SW-705).
- Interrupted downloads continue where they stopped, and nothing is downloaded twice.
- The previous version is kept. If a new version fails to start twice, SyncWatch goes back by itself (SW-707). You can also go back by hand in Settings > Updates.
- **Test versions** (advanced setting): try changes before everyone else gets them.

**Easy install for friends**
- The new all-in-one zip brings its own Python: unzip, double-click `SyncWatch.bat`, done. No Python install, no console window.
- Films live in `SyncWatch\movies`, outside the app folder, so updates never touch them.

**For the publisher**
- `tools\release.bat` creates your signing key (24 words on paper + passphrase), checks your paper backup, and publishes releases.
- Automatic tests run on GitHub's Windows and Linux machines for every change: the real window, two apps in sync, the whole update cycle, and the portable runtime.

## 0.2.1 (fixes)
- Setting explanations (ⓘ) now appear when you point at the ⓘ or click it. They didn't appear on Windows in 0.2.0.
- New option in Settings > Interface: "Show explanations under the settings" writes them under each setting permanently.
- Ticks in checkboxes and menus are black on a light box, so they're easy to see. Colours are in `assets/theme.json`: `check_box`, `check_border`, `check_mark`.
- Placeholder background is now a very dark blue.

## 0.2.0 (step 1: foundation and new look)
**Sync**
- No more automatic correcting while playing. That caused the stutter in 0.1. The gap is shown instead, and you fix it when you want:
  - **Sync me** jumps only you to your friend.
  - **Sync up** pauses everyone, lines up on the host and resumes together after a countdown.
- The host's app decides. Friends' presses are requests, with cooldowns the host's app enforces (play/pause 2 s, Sync up 15 s). Skipping can be spammed: it's sent once, when you stop pressing.
- "Only I can control playback when I host" switch, and "On my own" mode for friends.
- Sync up waits for each PC to really have the picture ready (up to 5 s), so slow PCs don't stutter.
- Names, profile pictures, a People panel with ping and gap, and an activity feed of who did what.
- Different film files are detected: identical, same length (±1 s, fine) or different (warning).

**Safety**
- Every message between the apps is strictly checked. Malformed data, floods or oversized messages close the connection (SW-309).
- The host accepts one friend; a second connection is refused instead of kicking your friend out (SW-310).
- A friend who silently drops off is noticed within 12 s. The port is bound exclusively on Windows.

**Look**
- New layout: video, sidebar and controls. All images live in `assets/` (placeholders for now) and colours in `assets/theme.json`.
- Seek bar with hover time and your friend's position as a marker. "Ends at" clock. Shortcut overlay (F1).

**Subtitles**
- Subtitles from .srt/.vtt/.ass files or from inside MKV films.
- Automatic language choice and encoding detection for Slovak/Czech files.
- Any delay, stretch for frame-rate mismatches, and adjustable size, colour, outline and box.

**Reliability**
- Errors have codes (SW-xxx) with plain explanations and **Copy details**.
- Staged startup checks, including a real invisible test video.
- Crash catcher: hard crashes are reported the next time you start (SW-990). Log files, Copy report.

**Settings**
- One settings window with basic and advanced options. Every change applies live and can be reset.
- Keyboard shortcuts are configurable.
- Settings and positions moved to `%APPDATA%\SyncWatch` (old positions are imported). Separate profiles for testing with two windows.

## 0.1.0
- First version: play a film, two PCs in sync over a direct connection.
