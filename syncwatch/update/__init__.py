"""Updates: finding, checking, downloading and installing new versions safely.

How an update gets from you to your friends:

1. You publish a release with ``tools/release.bat``. It uploads to GitHub:
   - ``manifest.json``: version, release notes, and the size and SHA-256 fingerprint of each file
   - ``manifest.json.sig``: your Ed25519 signature of the manifest, made with your signing
     key, which never leaves your PC
   - ``SyncWatch-<version>-app.zip``: the app itself (small)
   - ``SyncWatch-runtime-<id>.zip``: SyncWatch's own Python and libraries. Only uploaded
     when it changed, which is rare.
   - ``SyncWatch-<version>-windows.zip``: everything in one zip, for new friends
2. Friends' apps download the manifest and its signature and check the signature
   against the public keys built into the app (``keys.py``). A manifest that
   isn't signed by you is refused (SW-703), so neither a hacked GitHub account
   nor anything in between can push code to your friends.
3. The app downloads the files the manifest lists and checks each one's
   fingerprint (SW-705 if it doesn't match), then unpacks them next to the
   running version (``app.new``).
4. On the next start, ``boot.py`` swaps the new version in and keeps the old
   one. If the new version fails to start twice, it goes back automatically
   (SW-707).

The modules: ``version`` (comparing versions), ``manifest`` (format and
signatures), ``layout`` (install folders), ``github`` (finding releases and
downloading), ``install`` (safe unpacking and staging), ``manager`` (all of
it together, for the UI).
"""
