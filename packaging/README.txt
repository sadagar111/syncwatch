SyncWatch: watch films in sync with friends
============================================

START
  Double-click SyncWatch.bat.
  (The first time, Windows may ask "Do you want to run this file?". Click Run.)
  Nothing needs installing: SyncWatch brings its own Python in the "runtime" folder.

FILMS
  Put films into the "movies" folder, or open them from anywhere with the Open button.

WATCHING TOGETHER
  One person clicks Host and sends their address; the others type it and click Join.
  If you're not on the same home network, all of you install Tailscale
  (tailscale.com) and use the 100.x.x.x address.

UPDATES
  SyncWatch tells you when a new version is out. Click Update; it downloads,
  checks the publisher's signature, and installs when you restart. If a new
  version doesn't start properly, SyncWatch goes back to the previous one by itself.

PROBLEMS
  Every problem shows a code like SW-304 with an explanation. Click "Copy details"
  and send that to whoever gave you SyncWatch.

  Your settings and picture are kept separately, in %APPDATA%\SyncWatch, so you
  can always unzip a fresh copy of SyncWatch over this folder without losing them.

FOLDERS (don't move things around inside)
  app\        the program            runtime\   SyncWatch's own Python
  movies\     your films             boot.py    starts SyncWatch, installs updates
