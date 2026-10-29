# Test checklist for 0.4.1 (jumps together)

Send me error codes with **Copy details**, or **Settings → Diagnostics → Copy report**.
Older checklists: `docs/TESTING-0.3.md`, `docs/TESTING-0.2.md`. Sections A–E below are the
0.4.0 checklist, still worth a quick run.

## 0.4.1: jumps while the film plays (two windows, or bots)
- [ ] Playing, A skips ahead: both pause at the new spot for a moment, then play together (no 3-2-1)
- [ ] B skips while playing: B waits at the new spot at once, then both play together
- [ ] Skip several times quickly: one wait at the end, not one per press
- [ ] Skip, then pause before it starts again: nobody starts playing
- [ ] **Sync up**, then pause during the 3-2-1: nobody starts playing
- [ ] `tools\bots.bat <code> --count 9 --lag-ms 800`: after a jump everyone starts together (the bots wait ~1 s)
- [ ] Settings → Watching together → *When someone jumps while the film plays* → **Keep playing**: jumps don't pause
- [ ] `tools\measure_jumps.bat` with one of your real films: send me the SUMMARY line

## A. Before releasing (in your Git folder)
- [ ] The relay is online (lesson 5) and `tools\release.bat set-relay <address>` is merged
- [ ] Settings → Watching together shows *Your ID* and the relay address

## B. Alone, with two windows (`tools\Test with two windows.bat`)
- [ ] Window A: **Host my room** → the room panel shows the code; **Copy code**
- [ ] Window B: paste the code under *Join a room* → **Join** → it says *Asking … to let you in*
- [ ] Window A shows *Waiting to join: **TestB** XXXX-XXXX · new person* → **Let in** → both show 2 people
- [ ] Play, pause, jump in A: B follows. Pause in B: A pauses (B's request)
- [ ] **Sync up** in B: countdown in both, they start together
- [ ] Close window B without leaving, start it again (same profile), join again: it gets back in **without** asking
- [ ] A: right-click TestB → **Remove from the room**: B says it was removed
- [ ] B joins again → A: **No ▾ → Block this person** → B can't ask again (Settings → Watching together → Unblock works)
- [ ] A: **New code…** with B in the room: B follows by itself; the old code leads nowhere

## C. Bots (in your Git folder)
- [ ] `tools\bots.bat <code> --count 9` → **Let all 9 in** → People (10)
- [ ] Play/pause/jump: all bots follow (their activity shows in the bots window)
- [ ] `--lag-ms 800`: bots show ~0.8 s behind; **Sync up** waits for them
- [ ] `--chaos`: random presses; cooldowns and "only the host" (Settings) behave

## D. Friends (two windows)
- [ ] In a room: right-click the other → **Add friend** → the other side **Accept**s
- [ ] Both leave. A hosts: B's Friends list shows *Hosting TestA's room* → **Join** → A still has to **Let in**
- [ ] B opens SyncWatch *after* A started hosting: the room still shows up (within seconds)
- [ ] **⋯ → Remove friend…** removes it on both sides

## E. With friends (after publishing 0.4.0)
- [ ] Their 0.3.1 updates to 0.4.0 with the button
- [ ] A real film night through the relay: everyone in sync, the gap shown, Sync up works
- [ ] Optional: your ethical-hacker friend gets `SECURITY.md` and `docs/ROOMS.md`

### Things that are expected (not bugs)
- Codes from the test relay on your PC end in `@http://127.0.0.1:…`; real ones don't.
- Someone with a friend's name but a different ID is shown as a *new person*: that's the point of the ID.
