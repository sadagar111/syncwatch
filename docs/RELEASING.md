# Releasing SyncWatch

Everything here runs from the project folder in the VS Code terminal.

## One-time setup
1. `tools\release.bat set-repo <owner>/<name>`: tells the app where releases are published.
2. `tools\release.bat keygen`: creates your **signing key**.
   - It shows **24 words**. Write them on paper, in order. They *are* the key.
   - It then asks for a **passphrase**, which locks the key file on this PC.
   - The key file: `%APPDATA%\SyncWatch\release\signing_key.pem`. Never in the project, never on GitHub.
   - Your **public** key is written into `syncwatch/update/keys.py`. That one is meant to be public.
3. `tools\release.bat check-backup`: type the words from your paper to prove the paper is right.
4. Commit `keys.py` and `source.py` through a pull request (GIT_GUIDE lesson 3).
5. For rooms (0.4 and later): put the relay online and run
   `tools\release.bat set-relay https://syncwatch-relay.<you>.workers.dev`, then commit
   `syncwatch/net/relay_url.py` the same way (GIT_GUIDE lesson 5).

## Every release
1. The version number is in `syncwatch/__init__.py` (`__version__ = "0.3.1"`), and
   `CHANGELOG.md` needs a section `## 0.3.1 …`. Its text becomes the release notes your
   friends see. Both normally arrive with my pull request.
2. Merge the pull request, then `git switch main` and `git pull`.
3. `tools\release.bat publish --dry-run` (optional: builds everything into `dist\`).
4. `tools\release.bat publish`. Type the version to confirm. Done.

Friends' apps notice the new version within a few hours (the check interval), or right away
with Settings → Updates → Check now.

### Test versions
Versions like `0.3.1-test.1` are **test versions**: `tools\release.bat publish --test`.
They're published as GitHub "pre-releases" and only offered to people who ticked
Settings → Updates → *Get test versions* (advanced). Use them to try a change on your own PC
(or with a friend who tests) before everyone gets it. The normal `0.3.1` later counts as
newer than all `0.3.1-test.N`.

### What gets uploaded
| File | What it is |
|---|---|
| `manifest.json` | Version, notes, and the size and SHA-256 fingerprint of every file |
| `manifest.json.sig` | Your signature of the manifest. Apps refuse anything without a valid one |
| `SyncWatch-<v>-app.zip` | The app (well under 1 MB). What updates download |
| `SyncWatch-runtime-<id>.zip` | Python + libraries (~125 MB). **Only when `runtime-spec/` changed**; otherwise the earlier one is reused |
| `SyncWatch-<v>-windows.zip` | Everything in one zip for **new** friends (normal versions only) |

The release tool only packs **committed** files (it uses `git archive`), refuses to publish with
uncommitted changes or when your `main` differs from GitHub's, runs the tests first, and finally
checks that GitHub serves exactly what it signed.

## Keeping the key safe
| Situation | What to do |
|---|---|
| Forgot the passphrase | `tools\release.bat restore`, type the 24 words, choose a new passphrase |
| New PC / reinstalled Windows | Same: `restore` with the 24 words |
| Lost the paper, still have the PC | `tools\release.bat show-words` (needs the passphrase), write them down again |
| Lost both paper and passphrase/file | Your friends' apps can't accept updates from a new key. Make a new key (`keygen --replace`), publish, and each friend downloads the new full zip once (their settings are kept) |
| Someone else may have your words or key file | Same as above, and tell your friends not to install updates until they have the new zip |

**Replacing a key while you still have the old one (rotation):** add the new public key to
`keys.py` (keeping the old one), publish that release signed with the **old** key, and only then
start signing with the new key. Every app picks up trust in the new key from that update.
