"""Public keys whose signatures SyncWatch accepts for updates.

These are *public* keys: safe to show anyone. The matching private key lives
only on the publisher's PC (made with ``tools/release.bat keygen``, backed up
as 24 words on paper). ``release.bat keygen`` writes the new public key into
this file; commit that change like any other.

More than one key may be listed. That's how a key gets replaced one day:
publish one update, signed with the old key, that lists both keys, and only
then start signing with the new key.

While the list is empty, updates are switched off (SW-711).
"""

TRUSTED_KEYS = [
    "HVFNGmaGKe/OmDNY3rvNeoMnLsWolkvMXYbIs0R8kQw=",
]
