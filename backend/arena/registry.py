"""Game registry. Import a game module and call :func:`register` (the game
modules do this at import time). The server imports :mod:`arena.games` once at
startup, which pulls every bundled game in."""

from __future__ import annotations

from arena.game import Game

_REGISTRY: dict[str, Game] = {}


def register(game: Game) -> Game:
    if game.meta.id in _REGISTRY:
        raise ValueError(f"duplicate game id: {game.meta.id}")
    _REGISTRY[game.meta.id] = game
    return game


def get(game_id: str) -> Game:
    try:
        return _REGISTRY[game_id]
    except KeyError:
        raise KeyError(f"unknown game: {game_id}") from None


def all_games() -> list[Game]:
    return list(_REGISTRY.values())
