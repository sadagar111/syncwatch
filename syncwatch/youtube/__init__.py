"""YouTube watch-along (from 0.5): links, finding the streams with yt-dlp, and the room's queue.

* links.py    turns whatever people paste (watch links, youtu.be, Shorts, timestamps) into a video ID
* resolve.py  runs yt-dlp in its own process and prints the stream addresses as JSON
* streams.py  what the player needs from that (and the mpv "EDL" that joins picture and sound)

The room only ever shares video IDs, never stream addresses: those only work for the PC that
asked for them, so every PC asks YouTube itself.
"""
