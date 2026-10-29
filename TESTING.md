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
