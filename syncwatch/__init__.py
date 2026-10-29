"""SyncWatch: watch local films in sync with friends.

The package is split so that everything except ``syncwatch.ui`` and
``syncwatch.app`` works without Qt. That keeps the logic (settings, errors,
subtitles, sync) testable on any machine and easy to reuse.
"""

__version__ = "0.4.1"
APP_NAME = "SyncWatch"

# Bumped whenever the messages SyncWatch apps send each other change in an
# incompatible way. A room only lets in apps with the same number (SW-413).
# 3 = rooms through the relay (0.4); 2 was the direct two-person connection.
PROTOCOL_VERSION = 3

# Oldest Qt for Python version that has the FFmpeg-based video backend.
MIN_PYSIDE6 = (6, 5)
