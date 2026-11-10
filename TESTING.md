# Testing 0.5.0: branch `v0.5.0-release` (the pull request into main)

Everything from the week, as you kept it (all of it): YouTube (Up next, playlists, YouTube's subtitles, vote to
skip), torrents, ideas and the film vote, chat and reactions, seek-bar pictures, Sunday's extras (ready check,
next episode, skip intro, night mode, sound delay, waiting for a loading video, pausing when a connection
drops) and the Windows crash fix. The checklists below say which branch each part was built on; all of
them are in this one.

1. **Get it:** in your SyncWatch folder, `git fetch` then `git switch v0.5.0-release`, then start `SyncWatch.bat`
   as usual (it checks for the new parts: yt-dlp, Deno, libtorrent). Your profile and films stay as they are;
   to try it without touching your own settings, start it with `--profile test`.
2. **One thing to set:** a free TMDB key for the Ideas window (D1 below). Everything else works out of the box.
3. **Alone (about 30 minutes):** Y1 and Y4 (YouTube, a playlist, subtitles), T1 (a torrent; a public-domain film
   from archive.org is a good one), D1 (Ideas), P1 (seek-bar pictures), and Sunday's: next episode, skip intro,
   night mode, sound delay.
4. **With a friend** (two PCs, or two windows on one PC with `--profile`): Y2–Y3 (YouTube together, the queue,
   vote to skip), C1 (chat, reactions), D2 (film vote), T2 (*Get it from …*; best between two homes), the ready
   check, waiting for a loading video, pausing when a connection drops.
5. **When something breaks:** the Activity panel says what happened; the log folder is in Settings >
   Diagnostics. Tell me in the chat which step and what you saw.
6. **When it's good:** merge the pull request on GitHub, then on your PC `git switch main`, `git pull` and
   `tools\release.bat publish` (or `publish --test` first, for friends who get test versions).

---

# Test checklist: pictures over the seek bar (built on branch `try/0.9-previews`)

## P1. Seek-bar pictures
- [ ] Open a film from your library, wait ~half a minute, hover over the seek bar: a picture of that moment
      with its time. Moving along the bar changes it smoothly
- [ ] A long 4K/HEVC film: does making them slow down playback? (They're made at a low priority)
- [ ] Open the same film again: the pictures are there at once (the cache)

---

# Test checklist: chat and reactions (built on branch `try/0.8-chat`)

## C1. Chat and reactions (two PCs, or two windows with `--profile`)
- [ ] In a room, the sidebar says *Activity and chat* with a box at the bottom. **T** jumps into it, **Enter**
      sends, **Esc** goes back. Messages arrive on the other PC within a second, with the name
- [ ] Fullscreen (or Tab to hide the sidebar): a friend's message shows over the video for a few seconds
- [ ] **😊** in the control bar and keys **1–8**: the emoji floats up on both PCs (with the sender's name on the
      other one). Typing digits in the chat box doesn't send reactions
- [ ] Emoji look right on Windows (colour emoji, not boxes)

---

# Test checklist: what should we watch? (built on branch `try/0.7-discover`)

It needs a free TMDB key: themoviedb.org → sign up → Settings → API → request a key (personal use,
fill in anything sensible) → copy the **API Read Access Token**. TMDB was out of reach where this was
built, so everything was tested against a stand-in; this list is the test with the real thing.

## D1. Ideas (alone)
- [ ] Settings → Discover → paste the token. Start screen → **💡 Ideas**: *For you* fills within a few
      seconds (the first time it looks up each film once), with posters and *Because you have …*
- [ ] Are the recommendations sensible for your films? Any of your films matched wrongly? (*Your films*
      shows what each file was taken for; send me the file names that went wrong)
- [ ] Click a film: length, rating, description, *Streaming on …* for your country; **Trailer** plays it;
      **Where to watch** opens TMDB's page; a film from your library has **▶ Play**
- [ ] *Trending*, *Find a film…*, **🎲 Pick one for me**
- [ ] A wrong key (change one letter): a plain message, nothing breaks. No key: *Your films* still lists the
      library, and the note says how to get a key

## D2. Film vote (two PCs, or two windows with `--profile`)
- [ ] Host a room, the other joins. Guest: **🗳 Film vote**, or *Suggest for the vote* on a film: the
      *Film vote* panel appears for both
- [ ] Both suggest films and vote (double-click; again = take it back). The panel shows the votes and
      *Have it: …* for a film that's in someone's library
- [ ] The host clicks **Close the vote**: both see *🏆 … won*; whoever has it gets **▶ Play it**
- [ ] Settings → Discover → *Tell the room which suggested films I have* off: you no longer show under *Have it*

---

# Test checklist: torrents (built on branch `try/0.6-torrent`)

Use a legal torrent, e.g. Big Buck Bunny or Sintel (Blender's open films) or a film from the
Internet Archive's public-domain collection: their pages have a magnet link or a .torrent file.

## T1. Streaming
- [ ] `git switch try/0.6-torrent`, `SyncWatch.bat` (installs libtorrent once). Startup checks: **Torrents: libtorrent installed**
- [ ] Paste a magnet link on the start screen → *Watch*: the activity says *Streaming <file>*, and the film starts after a
      short while; the control bar shows *⬇ … % · … MB/s · … people*
- [ ] Jump to the middle, then near the end: it continues after a short wait, not after the whole download
- [ ] Windows asks once whether SyncWatch may use the network: allow it (or turn off *Let others connect to me*)
- [ ] Close the film (or open another): the download stops. *Keep the film*: it's in `movies\Torrents`; *Delete it*: gone
- [ ] Drop a .torrent file onto SyncWatch; Ctrl+V with a magnet link in the clipboard: both start streaming
- [ ] A film streamed to the end, then closed: it's in your library now (*Your films*)

## T2. In a room (two PCs, ideally in different homes)
- [ ] The host streams a magnet link: the guest's sidebar asks *Stream it too* → click: it streams and lines up
- [ ] The host plays a film from the library that the guest doesn't have: the guest sees *Get it from <host>*
      in the sidebar → the host is asked → the film plays for the guest while it downloads (control bar: ⬇ …)
- [ ] The host's control bar shows *⬆ sharing … · … people*. Send me how fast it was and whether you're on
      the same Wi-Fi or in different homes (the most interesting test: different homes)

---

# Test checklist: YouTube together (built on branch `try/0.5-youtube`)

Nothing here could be tried with the real YouTube from where it was built (YouTube is blocked
there); GitHub's machines try it on every run (see the notes on the checks page). So this list
is the real test.

## Y1. On your PC
- [ ] `git fetch` then `git switch try/0.5-youtube`, double-click `SyncWatch.bat`: it installs yt-dlp and
      Deno once (about 45 MB). The startup checks show **YouTube: yt-dlp 2026.10.26 with Deno**
- [ ] Start screen → YouTube → paste a link → **Watch**: the video opens within a few seconds, with sound
- [ ] The same with a youtu.be link, a Shorts link and a link with `&t=1m30s` (starts at 1:30)
- [ ] Ctrl+L while a film plays: a small window for the link. Ctrl+V outside text fields: opens the link
      in the clipboard. Drag a video's link from the browser onto SyncWatch: it opens
- [ ] Jump around, pause, play: as smooth as with a film? Settings → YouTube → *Quality* 720p / 4K: does it change?
- [ ] An age-restricted video: **SW-603** with a plain explanation. A private or deleted one: **SW-602**

- [ ] Sound delay (branch `try/0.5-sound-delay`, or `try/all`): with Bluetooth headphones, Alt+Z a few times
      until lips and words match (a toast shows the delay); it's kept after a restart
- [ ] Next episode (branch `try/0.5-next-episode`, or `try/all`): open an episode of a show (S01E01…), jump
      near its end: the next one opens and plays by itself; N jumps to it right away
- [ ] Night mode (branch `try/0.5-night-mode`, or `try/all`): an action film, Alt+N: explosions quieter, talking
      easier to hear (a toast says on/off); kept after a restart
- [ ] Skip intro (branch `try/0.5-skip-intro`, or `try/all`): an anime or show episode whose file has chapters
      (mpv shows them as marks; often "Opening"): during the opening, *Skip intro ⏭* and a hint; click or I

## Y2. Together (two PCs, or two windows with `--profile`)
- [ ] Ready check (branch `try/0.5-ready-check`, or `try/all`): the host clicks *✋ Ready check* under People;
      the guest sees *✓ I'm ready* and who's still missing; after the click the film starts with 3-2-1 for both
- [ ] Next episode together: both have the show's episodes (any file names). The host goes on to the next
      episode (N or at the end): the guest's copy of it opens and both start it together
- [ ] Pause when a connection drops (branch `try/0.5-pause-on-drop`, or `try/all`): while you watch, the guest
      pulls their network cable / turns off Wi-Fi for ~15 s: the host's room pauses (*no word from …*); when
      it's back, a few seconds later everyone starts together
- [ ] Host a room, the other joins. The host pastes a link: the other opens the same video by
      itself and lines up (People list: *in sync*)
- [ ] Play, pause, jumps, Sync up: same as with films
- [ ] The guest clicks *Following the room* (→ *On my own*): a new video from the host doesn't open for them
- [ ] The host switches to another video: everyone follows
- [ ] Waiting for a loading video (branch `try/0.5-wait-loading`, or `try/all`): the guest limits their internet
      (e.g. a phone hotspot, or a 4K video on slow Wi-Fi) until their video stops to load: after a few seconds
      everyone pauses with *Waiting for …: the video is still loading there*, and all start together when it's back

## Y3. The queue (Up next)
- [ ] Alone: ➕ Queue three videos while one plays. *Up next* shows them, titles appear after a moment
- [ ] When the video ends, the next one starts by itself; Skip jumps to the next; right-click: Play now,
      Move up/down, Remove
- [ ] In a room: the guest adds a video (➕ Queue): it appears for everyone. When a video ends, the next
      one starts for both **at the same moment** (the slower PC is waited for)
- [ ] The guest tries Skip: *Only the host can do that* (SW-610). Host: Settings → YouTube → *Who can skip*
      → Everyone: now the guest's Skip works
- [ ] Queue a deleted/private video: it's skipped with a line in Activity, the next one plays
- [ ] Vote to skip (branch `try/0.5-voteskip`, or `try/all`; three of you): a guest's Up next shows *Vote to skip*;
      after one vote it shows (1/2), after the second guest's vote the next video starts for everyone
- [ ] Paste a playlist link (a music album, or any `…&list=…` link): it asks, then the other videos appear in
      Up next with their titles; a playlist of more than 50 says how many didn't fit

## Y4. YouTube's subtitles (branch `try/0.5-captions`, or `try/all`)
- [ ] A video with subtitles from its uploader in one of your languages (e.g. a TED talk): after a few
      seconds they appear by themselves. CC menu: *YouTube · English* is ticked
- [ ] CC menu on a video without uploader subtitles: *YouTube, automatic · English (Original)* and the
      automatic ones in your languages (e.g. Slovak, translated). Pick one: it appears within a few seconds
- [ ] Open the same video again: its subtitles come at once (kept for a week)
- [ ] Timing… (delay) works on them like on a film's subtitles

---

# Test checklist for 0.4.2 (mpv and the film library)

Send me error codes with **Copy details**, or **Settings → Diagnostics → Copy report**.
Older checklists: `docs/TESTING-0.4.md` (rooms, friends, jumps), `docs/TESTING-0.3.md`,
`docs/TESTING-0.2.md`.

The automatic tests play films with mpv on Linux and Windows, but without a screen. What
only a real Windows PC can show is section 1: the picture, the mouse and fullscreen.

## 0. Getting it onto your PC
- [ ] In your Git folder (on the pull request's branch, or `main` after merging): double-click
      `SyncWatch.bat`. The first start installs mpv and PyAV, then gets the video player library
      (about 50 MB, once). If it says it can't get libmpv, see "libmpv" in the pull request.
- [ ] The startup checks (Settings → Diagnostics → *Run the checks again*) say
      **Video player: mpv 0.41.0 · own window · hardware decoding on** and **Test video: played fine**

## 1. mpv plays films (the part I couldn't see myself)
- [ ] Open a film: picture, sound and subtitles (SyncWatch's own) are fine
- [ ] The film with AC3 sound that gave SW-205 before: plays, with sound
- [ ] **Double-click the video**: fullscreen; Esc leaves it. The **mouse wheel** over the video changes the volume
- [ ] In fullscreen: subtitles, the 3-2-1 and messages still show; the mouse pointer hides after a moment
- [ ] Click on the video, then Space / arrows / F / M: the shortcuts still work
- [ ] Drop a film file onto the video: SyncWatch opens it (mpv doesn't take it over)
- [ ] Subtitles menu → *Audio track*: tracks show language and format (e.g. *English · AC3 5.1*); switching works
- [ ] A big film (4K or HEVC) if you have one: smooth? If not, try Settings → Playback → *Hardware video decoding* → Off
- [ ] Settings → Playback → *How mpv draws the picture* (advanced) → **OpenGL** → it offers to restart →
      the same checks: picture, double-click, wheel, **fullscreen with subtitles**
- [ ] Settings → Playback → *Video player* → **Qt** → restart: everything like 0.4.1. Then back to mpv
- [ ] `tools\measure_jumps.bat` with one of your real films: send me the SUMMARY line

## 2. The library
- [ ] The start screen lists the films in `movies\`, on a card in the middle; double-click plays one
- [ ] The card stays in the middle when you make the window bigger, smaller, or maximise it
- [ ] A first start fits your screen, in the middle: `SyncWatch.bat --profile screentest` (a new, empty
      profile; your own settings aren't touched. Afterwards you can delete
      `%APPDATA%\SyncWatch\profiles\screentest`)
- [ ] **Add film…** → a film in Downloads: *Checking…*, then *Copying…* with MB, speed and time left; then it
      opens, it's in the list and in `movies\`, and the original is still in Downloads
- [ ] **Add film…** with the same film again: "already in your library", no second copy
- [ ] **Cancel** while copying a big film: nothing is left in `movies\` (no `.part` file)
- [ ] Right-click your AC3 film → **Convert the sound…**: progress with time left; then
      *<name> (converted).mkv* plays, with sound, and it's as long as the original
- [ ] Right-click → **Remove from library…** (also the film that's open right now): it lands in the Recycle Bin
- [ ] 📂 or Ctrl+O while a film plays: the Library window
- [ ] Settings → Playback → *Library folder* → another folder: the list follows

## 3. Together (two windows, or a friend)
- [ ] Both on mpv: play, pause, jumps and Sync up stay together (People list: *in sync*)
- [ ] One with the converted copy, one with the original: *"…a different file of the same length. It should
      line up."* and it does
- [ ] One window on mpv, the other on Qt (*Video player* setting): still together

## 4. After publishing
- [ ] 0.4.1 updates to 0.4.2 with the button: the download is bigger once ("includes a new video engine"),
      and after the restart the startup checks say *Video player: mpv*
- [ ] A new friend: the full zip unzips and plays a film with nothing else installed

### Expected (not bugs)
- With *Keep playing* (jumps), mpv learns how long its jumps take on your PC, separately from the old
  player. The first few jumps may land a little behind.
- Choosing *Video player* or *How mpv draws the picture* asks to restart SyncWatch: they can't change
  while a film plays.
