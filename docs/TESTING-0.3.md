# Test checklist for 0.3.0 (updates and easy install)

You can test all of this alone. Friends only come in at the very end.
Send me error codes with **Copy details**, or **Settings → Diagnostics → Copy report**.
The 0.2 checklist is in `docs/TESTING-0.2.md`.

## A. Git lessons 1–3 (docs/GIT_GUIDE.md)
- [ ] 0.2.1 is on GitHub as your first commit (lesson 1)
- [ ] My pull request "SyncWatch 0.3.0" shows green checks, and you merged it (lesson 2)
- [ ] `main` is protected, and your signing key and repo name are merged through your own pull request (lesson 3)
- [ ] `tools\release.bat check-backup` says your 24 words are correct

## B. The automatic tests on GitHub
Repo → **Actions** → the newest run → **Artifacts** at the bottom → download `gui-windows-latest`.
- [ ] Open the screenshots in `ui/`. That's the real app, started on GitHub's Windows machine.
      Do they look like your app? (They're taken without a screen, so the video area is empty.)

## C. Your source-code copy (your Git folder)
- [ ] `SyncWatch.bat` installs `cryptography` once, then starts as before
- [ ] Settings → **Updates** says "source-code copy (update with git pull)"

## D. Your first release (lesson 4)
- [ ] `tools\release.bat publish --dry-run` finishes. The first time it builds the runtime (a few minutes); look into `dist\v0.3.0`
- [ ] `tools\release.bat publish` publishes 0.3.0; the release page shows 5 files
- [ ] Download `SyncWatch-0.3.0-windows.zip` and unzip it to e.g. `Documents\SyncWatch-test`
- [ ] Start that copy's `SyncWatch.bat`: **no console window**, the app works as before, and Settings → Updates says "installed copy"
- [ ] Its films folder is `SyncWatch-test\movies`
- [ ] Your settings, name and picture carried over: they're stored in your user profile, shared by both copies

## E. The update cycle (I'll send version 0.3.1-test.1 as a pull request)
- [ ] Merge it, `git switch main`, `git pull`, `tools\release.bat publish --test`
- [ ] In the **installed** copy: Settings → Updates → tick *Get test versions* (advanced) → **Check now**
- [ ] The banner says 0.3.1-test.1 is available; **What's new** shows the notes
- [ ] **Update now**: progress with speed and time left, then "ready"
- [ ] **Restart now**: SyncWatch comes back as 0.3.1-test.1 (sidebar shows the version, and the activity feed says "Updated to…")
- [ ] Settings → Updates → **Go back to version 0.3.0**: it restarts as 0.3.0
- [ ] **Check now** offers 0.3.1-test.1 again → update again

## F. Then: your friends
- [ ] Send friends the link to `SyncWatch-0.3.0-windows.zip` from the release page.
      From now on they update with the button.
- [ ] Optional: give your ethical-hacker friend `SECURITY.md` and the repo link

### Things that are expected (not bugs)
- Windows may ask "Do you want to run this file?" for `SyncWatch.bat` from a downloaded zip. Click Run.
- Antivirus may scan the new zip for a while on first start, so the first start can be slower.
