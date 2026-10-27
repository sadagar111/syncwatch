# Step 1 test checklist (version 0.2.1)

**New in 0.2.1, quick checks:**
- Settings: point at an ⓘ and its explanation pops up after a moment; clicking the ⓘ also works.
- Settings > Interface: "Show explanations under the settings" writes them under every setting.
- Ticks in checkboxes (Settings) and in the CC menu are black on a light box.
- The background is very dark blue.

The full step 1 checklist follows.

Everything here can be tested **alone on your PC**. Section G uses two SyncWatch
windows on one PC instead of a second computer.

**When something is wrong**, send me one of these:
- the **error code** and the text from **Copy details** in the popup, or
- a short description plus **Settings → Diagnostics → Copy report**.

Your settings, picture, logs and saved positions live in
`%APPDATA%\SyncWatch\profiles\default` (paste that into the Explorer address bar).
They're kept separately from the app, so replacing the app folder never loses them.

---

## A. Install and start
1. Unzip into a **new** folder and keep your old SyncWatch folder as a backup. Copy your film into `movies`.
2. Double-click **SyncWatch.bat**. You already have PySide6, so nothing should install.
3. Expected: a welcome window asking for your name, an optional picture and the films folder. **Start watching** stays greyed out until you type a name.
4. Optional: to see the startup checklist once, turn on Settings → *Show advanced options* → Interface → *Show the startup checks* → **Always**, then restart. Every row should be ✓, including *Test video: played fine* and your sound device.

## B. Look and design files
1. Placeholders: a blue background, a red triangle logo, grey circle avatars.
2. Replace `assets/logo.png` with any PNG and restart. Your image appears in the sidebar and as the window icon.
3. Rename `background.png` to something else and restart. You should get a plain dark background, no crash, and a line in the log (SW-111). Rename it back.

## C. Playback
- Open a film (📂 button, drop the file onto the window, or the only film in `movies` opens automatically).
- Play/pause button and **Space**; skip buttons and **← →** (Shift = 60 s); the seek bar (click, drag, hover shows the time).
- Volume slider, **↑ ↓**, mouse wheel over the video, **M** to mute. Volume is remembered after a restart.
- **F** or double-click for fullscreen. Controls and cursor hide after 2.5 s and come back when you move the mouse. **Esc** leaves fullscreen.
- **F1** shows the shortcut list. **Tab** hides the sidebar.
- Bottom right: length · time left · "ends 23:41".
- Close and reopen: the film continues where you stopped (activity says so).
- **Please check:** does the picture show normally, or does the video area stay black?

## D. Subtitles (CC button)
- A film with subtitles inside an MKV: the CC menu lists the tracks, and your language (sk, then cs, then en) is picked automatically. Subtitles appear almost immediately, even while the film is still being read (progress and time left show in the CC menu).
- A `.srt` next to the film with the same name, e.g. `Film.sk.srt`, is listed and picked if its language ranks higher.
- **Load a subtitle file…** works too, and so does dropping an .srt onto the window.
- An old Slovak/Czech .srt (Windows-1250) shows č, ř, ľ, ť correctly.
- **Z / X** move subtitles 0.1 s earlier/later. **Timing…** accepts any delay (e.g. −37.5 s) and has the stretch fix.
- Settings → Subtitles: size, colour and dark box apply live. With advanced options on: outline, font, distance from bottom.
- A Blu-ray "picture" subtitle track says "(can't be shown)" and explains SW-253. That's expected.
- **Please check:** do subtitles sit correctly over the video, including in fullscreen?

## E. Profile picture
Settings → Profile → **Change picture…**: drag, zoom (wheel or slider), rotate, *Use this picture*. It then shows in the People panel.

## F. Try to break it on purpose
| Do this | Expected |
|---|---|
| Rename any non-video file to `test.mkv` and open it | SW-205 popup |
| Type `abc def` into Join | SW-303 |
| Join `127.0.0.1` while nobody is hosting | SW-305 |
| Start SyncWatch.bat a second time | SW-108 ("already open") |
| Click **Copy details** in any popup and paste it into Notepad | code, technical line, versions |

## G. Watching together, alone (two windows)
1. Run **tools\Test with two windows.bat**. Each window asks for a name the first time (e.g. A and B). Mute one window.
2. Window A: **Host**. Window B: type `127.0.0.1` → **Join**. Both open the same film.
3. Check:
   - B jumps to A's position when joining.
   - Play/pause from **both** windows work.
   - In B, press pause twice quickly: the second press says "Please wait a moment" (2 s cooldown). A has no cooldown.
   - Press skip 5× quickly in either window: the other one jumps **once**.
   - The People panel shows both, with ping. The gap ("in sync" / "1.4 s behind") updates every 10 s.
   - **Sync up**: both pause, line up, 3-2-1 countdown, both play.
   - **Sync me**: only that window jumps.
   - B: click **🔗 Following the room** → becomes **👤 On my own**. Now B's buttons only affect B. **Sync me** rejoins.
   - A: Settings → Watching together → *Only I can control playback when I host*. B's pause now only pauses B.
   - Close B: A's activity shows "disconnected". Reopen and rejoin.
4. **Please check:** is there any stutter while playing? (There's no automatic correcting anymore, so there shouldn't be.)

## H. Optional: one real test with your friend
Same as G over Tailscale. Your friend needs this version too: different versions refuse to connect (SW-308), which is intended.

---

### Known limits of this step (coming later)
- Two people at a time; rooms for up to 10 come in step 3.
- Still connects directly (Tailscale / same network, firewall script for the host).
- Updates still mean sending the zip around; the update button comes in step 2.
