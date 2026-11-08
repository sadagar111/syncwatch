# Changelog

## 0.5.0 (in progress: YouTube together)
**Watch YouTube together**
- Paste a YouTube link on the start screen (or press Ctrl+L, or Ctrl+V anywhere, or drag a link from your browser onto SyncWatch). Watch links, youtu.be, Shorts and `&t=` start times all work.
- In a room, when the host plays a YouTube video, everyone who follows the host opens it too and lines up with the host. Play, pause, jumps, Sync up and Sync me work as with films.
- **Up next (the queue):** ➕ Queue next to Watch (or Ctrl+V / dropping a link while a video plays) lines videos up in the sidebar. When one ends, the next starts for everyone at the same moment: SyncWatch waits until everyone has it open (at most 20 s, then it names who's slow). Skip, and right-click a video: Play now, Move up/down, Remove. Titles fill in by themselves. A video that's gone from YouTube is skipped with a line in the activity feed.
- **Playlists:** paste a playlist link (`…playlist?list=…`, or a video link with `&list=…`): its video plays and SyncWatch asks whether to put the playlist's other videos into Up next (at most 50; Settings > YouTube: ask / all / only the one video). Private and deleted videos are left out. The host (or someone alone) adds whole playlists; a member's link adds only its one video.
- The host decides who may add videos and who may skip, remove and reorder (Settings > YouTube). Everyone can remove the videos they added. The host's app checks every request, like everything else in a room.
- **Pause when someone's connection drops:** if a friend following the room goes silent while you watch (their internet or PC stops answering for 6 s), everyone pauses with a line saying why. When they're back (and steady for a few seconds), everyone starts together; if they don't come back, the host decides. When you host: Settings > Watching together > Pause when someone's connection drops (on).
- No ads: SyncWatch plays YouTube's video streams directly with mpv (through yt-dlp). Quality up to 1080p by default (Settings > YouTube > Quality, up to 4K).
- Nothing to install: yt-dlp and Deno (which yt-dlp needs for YouTube) come with SyncWatch. That makes this update about 45 MB bigger (once).
- New problems with plain explanations: SW-601 to SW-609 (e.g. "YouTube wants a sign-in for this video", "YouTube changed something: update SyncWatch").

## 0.4.2 (new video player, and your film library)
**A new video player: mpv**
- Films now play with **mpv**, which handles practically every film, including AC3, E-AC3 and DTS sound that didn't always play before.
- It comes inside SyncWatch, nothing to install. That's why this update is bigger than usual (once).
- The previous player stays as a fallback: SyncWatch switches to it by itself if mpv can't start on a PC (SW-209), and you can pick it in Settings → Playback → *Video player* (advanced).
- Also in Settings → Playback (advanced): *How mpv draws the picture* (try OpenGL if the picture stays black) and *Hardware video decoding*.
- The audio track menu shows each track's format, e.g. *English · AC3 5.1*.

**Your film library**
- The start screen lists your films, on a card in the middle of the window. Double-click one to play it; 📂 or Ctrl+O shows the list while a film is open.
- **Add film…** picks a film from Downloads or anywhere on your PC. SyncWatch checks that it plays, then copies it into your library folder with a progress bar and the time left. The original stays where it was, and a film that's already in the library isn't copied twice.
- If a film's picture plays but its sound doesn't, SyncWatch offers to **convert the sound** (to AAC). The picture is copied untouched, so it's quick, and the copy is exactly as long as the original: friends who have the original still line up with you.
- Right-click a film (or *More*): Show in folder, Convert the sound…, Remove from library… (to the Recycle Bin).
- Dropping a film file onto SyncWatch still plays it straight away, without adding it.
- *Films folder* is now *Library folder* (Settings → Playback). SyncWatch no longer opens a film by itself at startup: pick one from the list.

**Other**
- **Fix:** the window no longer opens wider than asked (it made room for the room buttons before you were in a room). The first time, it fits the screen and opens in the middle, also on a laptop with large display scaling.
- Scroll bars match the dark look.
- SyncWatch is now officially free software (GNU GPL 3 or later): `LICENSE.txt`. `THIRD_PARTY_NOTICES.txt` lists everything inside, with where its source code is.
- `tools\measure_jumps.bat` measures mpv (or the previous player with `--player qt`).

## 0.4.1 (jumps together)
- **Jumping while the film plays keeps everyone together.** Everyone waits at the new spot until every PC shows it, then all play at the same moment, so nobody misses the first seconds after a jump. Slow PCs no longer end up behind.
- Or choose *Keep playing* (Settings → Watching together → *When someone jumps while the film plays*): everyone jumps straight away, and each PC learns how long its own jumps take and jumps that much further ahead. The host's choice counts for the whole room.
- **Fix:** pausing during a Sync up countdown (or while the room waited after a jump) now stops everyone. Before, the others started playing anyway.
- **Fix:** whoever jumped while the film played no longer ends up about 0.3 s ahead of everyone.
- `tools\measure_jumps.bat`: drag a film onto it to see how long jumps take on your PC.

## 0.4.0 (step 3: rooms, friends and security)
**Rooms for up to 10 people (or 20)**
- **Host my room** opens your room; **Copy code** gives you its code (`SW-…`). Friends paste it under *Join a room*. No addresses, no Tailscale, no firewall settings: everything goes through your relay server.
- Your room keeps its code. **New code** makes a new one: the old code stops working, and people in the room follow by themselves.
- Everyone who asks to join waits for you: **Let in**, **Not now**, or **Block** (*When someone asks to join my room* in Settings: ask every time, friends join directly, only friends, or anyone).
- After a dropped connection or a restart, people who were just in your room get back in by themselves (within 10 minutes; can be turned off). Closing your room on purpose ends that.
- The **People** list shows everyone with their picture, whether they're playing, paused, loading or watching on their own, how far apart they are, and whether someone has a different film. Right-click someone: Add friend, Remove, Remove and block, Copy their ID.
- Watching together works as before, for the whole room: play, pause, jumps, **Sync up** (waits for everyone, names whoever was too slow), **Sync me**, host-only control, cooldowns per person.
- Gaps are measured on the host's clock, so they're right even when the PCs' clocks differ.
- *Compare my position with* (advanced): the host, or the median or average of everyone else.

**Friends**
- In a room, **Add friend**; the other person accepts. Friends see when you host (**Hosting · Join**), and rooms that are already open show up when SyncWatch starts. Joining still needs the host's OK.
- *Tell friends when I'm hosting* and *Show friends' rooms* can be switched off.

**Security** (docs/ROOMS.md explains the design)
- Every PC has its own keys; people see your name with a short ID like `K7QF-2M9X`, so nobody can pass as you by using your name.
- Every message is encrypted and signed. The relay only passes on scrambled data.
- A room code alone only lets someone ask. The room key changes whenever someone joins or leaves, so a leaked code or someone who left can't read along.
- Only the host's app can send commands for everyone; a member's modified app can't.
- Broken, random or flooding messages, from someone in the room or anyone with the code, can't crash anyone's app or make the host flood the room (`tests/test_fuzz.py` tries thousands of them).

**For testing**
- `tools\bots.bat <room code> --count 9` fills your room with fake viewers (`--lag-ms`, `--drift`, `--chaos`).
- The automatic tests now run three real apps in one room.

**Removed**
- The direct connection (host address, port 50505, Tailscale). It may come back later as an option.
- `allow_firewall.bat`: SyncWatch only makes outgoing connections now. If you ran it once, `remove_firewall.bat` (in `app\tools` in the zip version) removes the rule it made.

## 0.3.1 (fixes)
- **Unzipping works with Windows Explorer.** It used to fail with "Path too long" (0x80010135) in folders with long names. The bundled Python leaves out parts of Qt that SyncWatch never uses, so it's also 73 MB smaller once unzipped and downloads faster.
- **The update window opens on top.** Pressing *What's new…* in Settings opened it behind the Settings window, so you had to close Settings first.

## 0.3.1-test.2 (test update: smaller runtime)
- **Fix:** unzipping `SyncWatch-…-windows.zip` with Windows Explorer failed with "Path too long" (0x80010135) when the target folder had a long name. Files SyncWatch never uses are now left out of the bundled Python (Qt's QML, translations, developer tools and build files), so the longest path inside is 91 characters instead of 169.
- The bundled Python is about 73 MB smaller once unzipped, and 3,400 files fewer.
- This update downloads the new bundled Python once, then switches to it. That tests the runtime switch too.

## 0.3.1-test.1 (test update)
A small test version, to try the whole update cycle once before friends rely on it.
- The version number in the sidebar says 0.3.1-test.1, so you can see the update worked.
- docs/GIT_GUIDE.md: how to tell PowerShell from Git Bash, and how to check you're in the repo folder.

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

**Fixes**
- When a third person tries to join, they now see "Your friend is already watching with someone" (SW-310) on Windows too, instead of "Network problem" (SW-312).

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
