SyncWatch: watch films in sync with friends
============================================

START
  Double-click SyncWatch.bat.
  (The first time, Windows may ask "Do you want to run this file?". Click Run.)
  Nothing needs installing: SyncWatch brings its own Python in the "runtime" folder.

FILMS
  The start screen shows your films (your library). Double-click one to play it.
  Add film... picks a film from Downloads or anywhere on your PC: SyncWatch checks that it
  plays and copies it into the "movies" folder (the original stays where it was). If its
  sound can't be played, SyncWatch offers to convert the sound (the picture isn't touched).
  To play a film without adding it, drop the file onto SyncWatch.

WATCHING TOGETHER
  One person clicks "Host my room" and sends the room code (Copy code).
  The others paste it under "Join a room" and click Join; the host clicks "Let in".
  Nothing else is needed: no addresses, no Tailscale, no firewall settings.
  Everything is encrypted, and only people the host lets in can see the room.

FRIENDS
  In a room, right-click someone in the People list and choose Add friend.
  Friends see when you host and can ask to join with one click.

UPDATES
  SyncWatch tells you when a new version is out. Click Update; it downloads,
  checks the publisher's signature, and installs when you restart. If a new
  version doesn't start properly, SyncWatch goes back to the previous one by itself.

PROBLEMS
  Every problem shows a code like SW-402 with an explanation. Click "Copy details"
  and send that to whoever gave you SyncWatch.

  Your settings and picture are kept separately, in %APPDATA%\SyncWatch, so you
  can always unzip a fresh copy of SyncWatch over this folder without losing them.

FOLDERS (don't move things around inside)
  app\        the program            runtime\   SyncWatch's own Python and video player
  movies\     your films (library)   boot.py    starts SyncWatch, installs updates

LICENCE
  SyncWatch is free software (GNU GPL 3 or later, LICENSE.txt). THIRD_PARTY_NOTICES.txt
  lists everything it brings along, with the licences and where their source code is.
