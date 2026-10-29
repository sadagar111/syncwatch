# SyncWatch security notes

For anyone testing or reviewing SyncWatch's security. Please report findings privately to the
maintainer, not in public issues.

## What SyncWatch does
A Windows desktop app (Python + Qt) that plays local video files and keeps the playback of up to
20 PCs in sync. Since 0.4 everyone meets in a **room** on a relay server (a Cloudflare Worker),
end-to-end encrypted and signed; the host lets people in. It updates itself from GitHub releases.
The full room design, with message formats and keys, is in [docs/ROOMS.md](docs/ROOMS.md).

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

### Rooms (since 0.4; details in docs/ROOMS.md)
- **Nothing is open on anyone's PC.** Every app makes an outgoing WebSocket (`wss://`) connection to
  the relay. The relay only passes encrypted messages between apps on the same 128-bit random
  *channel*; it stores and logs nothing, and isn't trusted with anything.
- **Identities:** each installation makes Ed25519 (signing) and X25519 (encryption) keys on first start
  (`identity.json` in the profile folder). People see a short ID like `K7QF-2M9X` next to names; the
  app compares full 256-bit fingerprints.
- **Room codes** contain a 128-bit room secret and a 64-bit commitment to the host's keys, so a code
  can't be used to pose as the host (SW-408). A code only lets you **ask** to join.
- **The host decides** who comes in: *Ask every time* (default), *Friends join directly*, *Only friends*,
  or (advanced) *Anyone with the code*. Blocked people are always turned away; at most 25 requests
  wait at once, and each identity can ask at most 6 times a minute.
- **Encryption:** every message is ChaCha20-Poly1305 encrypted *and* Ed25519 signed, bound to its
  channel, room key epoch and recipient. The room key changes whenever someone joins or leaves, so
  people who weren't let in, left or were removed can't read along. Counters per sender session
  stop replays.
- **Permissions:** commands that change playback for everyone, the member list and the Sync up steps
  are accepted only from the host. Members send *requests*, which the host's app checks (rules,
  cooldowns, seek rate) before passing them on. Statuses (where each person is in the film) are
  visible to everyone in the room.
- **Strict parsing:** every message body is checked against a table of allowed types, fields,
  sizes and ranges (`syncwatch/net/protocol.py`) before use; unknown or malformed messages are
  dropped. Received text loses control characters, lone UTF-16 halves and invisible or
  direction-changing characters (so a name can't hide or flip the text after it), and is never
  shown as HTML. Profile pictures: at most 40 KB, PNG or JPEG only, at most 512 px (checked
  before Qt decodes the pixels). `tests/test_fuzz.py` throws random and hostile data at all of
  this, including from someone inside the room.
- **Floods:** the relay limits every connection; the host answers each person only a little
  (one refusal or pong per second, member lists at most once a second, refusals to strangers
  5 a second), so nobody can make the host flood the room.
- **Friends** find each other's rooms through *mailboxes*: relay channels only friends know. Mailbox
  messages are sealed to the recipient, signed, and dropped unless they're signed with a friend's
  key and carry a time within 10 minutes of the receiver's clock (the one exception: the answer to a
  friend request you sent). A friend can only point to their own room (the code's host commitment
  must be theirs). Joining from the friend list still needs the host's approval.
- **People who were just in the room** (dropped connection, restarted app) get back in without
  asking for 10 minutes, recognised by their full fingerprint; removed or blocked people don't, and
  the list is cleared when the host closes the room.
- **What the relay operator (or anyone watching it) can see:** that channels exist, how many apps are
  connected, message sizes and timing, IP addresses. They can drop or delay messages (denial of
  service) but not read, change, invent or replay them.
- **Known gaps:** `identity.json` and `rooms.json` aren't encrypted at rest: someone who can read
  your profile folder can act as you. Messages sealed to a person (the room keys in `welcome` and
  `rekey`, mailbox messages) have no forward secrecy: whoever recorded the encrypted traffic (the
  relay operator could) and later steals that person's `identity.json` could read them. And a
  member can always record what they see while they're in the room.

### Files from the internet
- **Films** are decoded by mpv (libmpv, with FFmpeg inside) since 0.4.2, or by Qt Multimedia's FFmpeg
  in the fallback player. A crafted file can exploit decoder bugs, as with any media player.
  Mitigation: keep the runtime updated (a new libmpv build comes with a release; see below).
- **Adding a film to the library** reads it twice more before copying: the built-in converter's
  "probe" (PyAV/FFmpeg) and a hidden test playback. The probe and every conversion run in a
  **separate process** (`syncwatch/library/convert.py`, started with `-I`, standard library + PyAV
  only), so a decoder crash only ends that process. Files are only ever written into the library
  folder (as `*.part` until complete), and only films inside the library folder can be removed
  (to the Recycle Bin).
- **libmpv itself:** SyncWatch loads `libmpv-2.dll` only from its own folders (the runtime's
  `libmpv\`, or `libmpv\` in a source-code folder), never from PATH or anywhere else
  (`syncwatch/mpvlib.py` answers python-mpv's DLL lookup with that one file). The DLL is pinned by
  its SHA-256 in `runtime-spec/runtime.json`; the build tools refuse any other file, whether it
  comes from the copy in the `deps-libmpv-…` pre-release or from the original build. That
  pre-release is ignored by the updater (no signed manifest), and the runtime zip that contains the
  DLL is listed with its fingerprint in the signed manifest like everything else.
- No mpv configuration, scripts or key bindings are loaded (`config=no`, `load-scripts=no`,
  `ytdl=no`, no input bindings): an `mpv.conf` or script elsewhere on the PC can't change what
  SyncWatch's player does.
- **Subtitles:** `.srt/.vtt/.ass` parsing is pure Python with size caps (10 MB). The MKV subtitle reader
  (`syncwatch/subtitles/mkv.py`) bounds-checks every element size against the file and caps
  decompression. Fuzzing it is welcome.

### Local
- Settings, logs and keys live in `%APPDATA%\SyncWatch`, never in the app folder or the repository.
  "Copy report" redacts token/password/key-like values; room codes aren't logged or reported, and
  relay channels appear in logs only shortened.
- Test-only environment variables (`SYNCWATCH_UPDATE_SERVER`, `SYNCWATCH_RELAY`,
  `SYNCWATCH_TEST_EXIT_AFTER`, `SYNCWATCH_DATA_DIR`) only matter to someone who can already run
  programs as the user.

## Good places to look
1. `syncwatch/update/` and `boot.py`: try to get unsigned code to run, or downgrade an installation.
2. `syncwatch/room/` (`session.py`, `friends.py`, `crypto.py`, `codes.py`): get into a room without the
   host's OK, read a room you were removed from, pose as the host or another member, replay old
   messages, or get into someone's friend list.
3. `syncwatch/net/websocket.py`, `relay.py`, `protocol.py`: crash or hang an app with what arrives
   from the relay. `tools/local_relay.py` is a relay on your own PC for experiments, and
   `tools/bots.py` fills a room with fake people.
4. `relay/src/index.js`: get the relay to leak or mix up channels, or to fall over cheaply.
5. `syncwatch/subtitles/mkv.py` and `parsers.py`: malformed files. `syncwatch/library/`: odd file names,
   films that change while they're copied, files that crash or hang the converter.
6. `tools/release.py`: anything that could make it sign something other than the committed code.

Please test against your own relay (`relay/README.md`) or `tools/local_relay.py`, not against
other people's rooms.
