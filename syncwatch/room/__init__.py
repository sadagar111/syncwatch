"""Rooms: watching with several people through a relay server, safely.

* ``codes``    room codes people copy and paste (with a typo check)
* ``identity`` each PC's own keys: who you are, provably
* ``crypto``   encrypting and signing every message (the relay only sees noise)

How it all fits together is described in docs/ROOMS.md.
No Qt in here, so all of it can be tested without a screen.
"""
