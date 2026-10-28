"""Which relay server rooms use by default.

Set once with ``tools\\release.bat set-relay https://syncwatch-relay.<you>.workers.dev``
after deploying the relay (docs/GIT_GUIDE.md, lesson 5). While it's empty,
rooms are switched off (SW-401).

It's only the default: Settings can point to another relay (advanced), and a
room code carries its relay's address when it isn't this one.
"""

RELAY_URL = ""
