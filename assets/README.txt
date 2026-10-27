SyncWatch design files
======================

Everything in this folder can be replaced to change how SyncWatch looks.
Keep the same file name, save as PNG, and restart SyncWatch.
If a file is missing or broken, SyncWatch uses a plain default and keeps working.

Current files are deliberately plain placeholders (a red triangle, a blue
rectangle, a grey circle) waiting for a real design.

FILE                 SIZE (suggested)        WHERE IT'S USED
logo.png             512 x 512, transparent  Window icon, top of the sidebar (shown about 28 px high),
                                             and the start screen (up to 256 px).
background.png       1920 x 1080 or larger   Behind the whole app and on the start screen.
                                             Scaled to fill the window; how is set in theme.json
                                             ("cover", "stretch", "tile" or "center").
default_avatar.png   256 x 256               Picture for people who haven't chosen one.
                                             Shown cropped to a circle, from 20 to 64 px.
icons/<name>.png     64 x 64, transparent    Button icons. A button shows its text label until a
                     (white glyph works best  PNG with its name exists. Names:
                     on the dark theme)       play, pause, back, forward, sync_up, sync_me, subtitles,
                                             volume, mute, fullscreen, exit_fullscreen, sidebar,
                                             settings, open, copy, host, join, leave, help
theme.json           (text file)             Colours, font and corner roundness. See "_help" inside.

selftest/selftest.mp4  Not a design file: a 2-second test video SyncWatch plays at startup to check
                       that video playback works. Please don't replace it.
