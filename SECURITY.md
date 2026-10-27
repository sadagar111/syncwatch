# SyncWatch security notes

For anyone testing or reviewing SyncWatch's security. Please report findings privately to the
maintainer, not in public issues.

## What SyncWatch does
A Windows desktop app (Python + Qt) that plays local video files and keeps two PCs' playback in
sync over a direct TCP connection. It updates itself from GitHub releases. Version 0.3 has **no
rooms or relay yet**; those come in step 3 with end-to-end encryption, signed messages and host
approval (see the end of this file).

## Threat model and defences

### Updates (the most powerful part, since they install code)
- **Signed manifests:** each release has `manifest.json`, signed with an Ed25519 key that exists
  only on the publisher's PC, encrypted with a passphrase, with a 24-word paper backup. The public keys are
  compiled into the app (`syncwatch/update/keys.py`). Unsigned or wrongly signed manifests are
  refused (SW-703) before anything is downloaded.
- **File integrity:** the manifest lists the size and SHA-256 of each file. Downloads that are too large
  are cut off; mismatches are thrown away (SW-705).
- **Binding:** the manifest must name the configured repository and match the release tag, and
  its version must be **higher** than the installed one. Replaying an old signed manifest can't
  downgrade anyone.
- **Transport:** `https` only. Redirects to non-https are refused; plain `http` is accepted only for
  `127.0.0.1` / `localhost`, which the test suite uses.
- **Unpacking:** paths are checked (no `..`, absolute paths, drive letters, `:`), symlinks are refused,
  and total size and file count are capped. Staging happens next to the live version and is swapped by
  `boot.py` at the next start.
- **Recovery:** a version that fails to start twice is rolled back automatically and not offered again
  automatically.
- **Publishing side:** `tools/release.py` only packs committed files (`git archive`), requires
  `main` to equal `origin/main`, runs the tests, and verifies what GitHub serves after uploading.
  `main` is protected: changes only through pull requests with passing checks.
- **GitHub account compromise:** an attacker can delete or hide releases (denial of service) but can't
  sign a manifest. **Known gap:** withholding updates ("freeze attack") isn't detected.

### Direct connection (step 1/2 design, being replaced in step 3)
- One TCP connection between host and friend. It's **not encrypted or authenticated** by SyncWatch
  itself; over Tailscale, the tunnel is encrypted and limited to tailnet members.
- Everything received is size-limited (256 KB per line), rate-limited (token bucket) and strictly
  validated (`syncwatch/net/protocol.py`: types, ranges, allowed values). Anything malformed closes the
  connection (SW-309). The host accepts one friend at a time. The port is bound exclusively on Windows.
- What a malicious peer *can* do: control playback within the host's rules, send a name and a small
  avatar (only PNG/JPEG, ≤ 512 px, decoded size-checked by Qt).

### Files from the internet
- **Films** are decoded by Qt Multimedia's FFmpeg. A crafted file can exploit decoder bugs, as with any
  media player. Mitigation: keep the runtime updated.
- **Subtitles:** `.srt/.vtt/.ass` parsing is pure Python with size caps (10 MB). The MKV subtitle reader
  (`syncwatch/subtitles/mkv.py`) bounds-checks every element size against the file and caps
  decompression. Fuzzing it is welcome.

### Local
- Settings, logs and keys live in `%APPDATA%\SyncWatch`, never in the app folder or the repository.
  "Copy report" redacts token/password/key-like values.
- Test-only environment variables (`SYNCWATCH_UPDATE_SERVER`, `SYNCWATCH_TEST_EXIT_AFTER`,
  `SYNCWATCH_DATA_DIR`) only matter to someone who can already run programs as the user.

## Good places to look
1. `syncwatch/update/` and `boot.py`: try to get unsigned code to run, or downgrade an installation.
2. `syncwatch/net/link.py`, `protocol.py`: try to crash or hang a host from a peer.
3. `syncwatch/subtitles/mkv.py` and `parsers.py`: malformed files.
4. `tools/release.py`: anything that could make it sign something other than the committed code.

## Coming in step 3 (rooms)
Encrypted relay (no open ports), per-installation identity keys (Ed25519/X25519), host approval and
whitelist, a session key rotated when someone is removed, signed messages (who shared what is proven),
replay protection, and host-enforced permissions and rate limits.
