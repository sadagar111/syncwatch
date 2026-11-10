"""What should we watch? Films you have -> recommendations, and the room's film vote.

names.py  a film file's name -> its title and year (no network)
tmdb.py   The Movie Database (TMDB): finding films, details, recommendations, trending; every
          answer checked, a small cache on disk (the key comes from Settings > Discover)
picks.py  "For you": recommendations worked out from the films you have and have watched
vote.py   the film vote in a room (held by the host's app, like the YouTube queue)
No Qt in here; ui/discover_ui.py is the window and runs the TMDB part in a background thread.
"""
