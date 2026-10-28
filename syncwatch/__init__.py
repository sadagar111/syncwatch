"""SyncWatch: watch local films in sync with friends.

The package is split so that everything except ``syncwatch.ui`` and
``syncwatch.app`` works without Qt. That keeps the logic (settings, errors,
subtitles, sync) testable on any machine and easy to reuse.
"""

__version__ = "0.3.1"
APP_NAME = "SyncWatch"

# Bumped whenever the messages two SyncWatch apps send each other change in
# an incompatible way. Apps with different numbers refuse to connect (SW-308).
PROTOCOL_VERSION = 2

# Oldest Qt for Python version that has the FFmpeg-based video backend.
MIN_PYSIDE6 = (6, 5)
