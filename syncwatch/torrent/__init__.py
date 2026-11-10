"""Streaming torrents (from 0.5): watch while it downloads, the part you're watching first.

* engine.py   libtorrent in a process of its own, plus a small web server (127.0.0.1 only, with a
              secret token) that hands the film to mpv and fetches the pieces mpv asks for first
* links.py    recognising magnet links and .torrent files

Only torrents people open themselves (a magnet link, a .torrent file); SyncWatch doesn't search
for films anywhere.
"""
