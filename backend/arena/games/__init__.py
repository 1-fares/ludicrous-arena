"""Importing this package registers every bundled game.

To add a game: drop a module in this directory that builds a :class:`Game` and
calls :func:`arena.registry.register`, then import it here. Nothing else in the
server needs to change -- discovery, lobbies, streaming and the viewer are all
driven off ``GameMeta`` and the Game protocol methods.
"""

from arena.games import deathmatch  # noqa: F401
from arena.games import lockdown    # noqa: F401
from arena.games import finance     # noqa: F401
from arena.games import skirmish    # noqa: F401
