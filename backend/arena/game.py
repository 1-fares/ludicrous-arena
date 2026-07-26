"""The Game extension point.

A game is anything that implements :class:`Game`. The engine owns the lifecycle
(ticking, broadcasting, persistence); a game only owns *rules*. Keep games pure:
mutate ``state`` in place, do no I/O, never block. The engine ticks every game on
a single event loop, so a slow ``tick`` stalls every match sharing the process.

State is an arbitrary object of the game's choosing (a dataclass, a dict,
whatever). The engine treats it as opaque. Because the server is serverless and
holds no match in memory between requests, the engine persists state in Tablestore
between every action: a game must therefore provide ``encode_state`` /
``decode_state`` to round-trip its state through JSON. Keep games deterministic so
the engine can fast-forward the simulation to "now" on demand (see engine.py).
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from arena.models import GameMeta, MatchResult, PlayerSlot


@runtime_checkable
class Game(Protocol):
    """Rules for one game type. Stateless across matches; ``init_state`` makes
    the per-match state object that the other methods receive back."""

    meta: GameMeta

    def init_state(self, config: dict[str, Any], players: list[PlayerSlot]) -> Any:
        """Build the authoritative state for a new match.

        ``config`` has already been merged with defaults and is trusted.
        ``players`` is the final roster (teams assigned). Late joins are not
        supported by default -- declare ``min_players == max_players`` or accept
        that the roster is fixed at start.
        """
        ...

    def validate(self, state: Any, player_id: str, action: dict[str, Any]) -> Optional[str]:
        """Return ``None`` if the action is legal, else a short error string.

        Rejected actions are dropped and the reason is echoed to the agent under
        the state envelope's ``seat.rejected`` (a sibling of ``observation``, not a
        field inside it). Never raise here -- a buggy agent must not be able to
        crash the tick.
        """
        ...

    def apply(self, state: Any, player_id: str, action: dict[str, Any]) -> None:
        """Apply a validated action to ``state``. Called at tick start, in the
        order actions were submitted across all players."""
        ...

    def tick(self, state: Any, dt: float) -> None:
        """Advance the simulation by ``dt`` seconds (1 / tick_rate). Physics,
        timers, AI, resource generation all happen here."""
        ...

    def observe(self, state: Any, player_id: str) -> dict[str, Any]:
        """The view handed to one agent. Apply fog-of-war / partial information
        here -- never leak what the player should not see. Must be JSON-safe."""
        ...

    def render(self, state: Any) -> dict[str, Any]:
        """The full scene handed to spectators / the three.js viewer. Omnniscient
        by convention (spectators see everything). Must be JSON-safe."""
        ...

    def result(self, state: Any) -> Optional[MatchResult]:
        """Return a :class:`MatchResult` once the match is decided, else ``None``.
        Checked after every tick; the first non-None result ends the match."""
        ...

    def encode_state(self, state: Any) -> dict[str, Any]:
        """Serialize ``state`` to a JSON-safe dict for persistence. Must be the
        exact inverse of ``decode_state``. Called on every write."""
        ...

    def decode_state(self, data: dict[str, Any]) -> Any:
        """Rebuild the in-memory state from ``encode_state``'s output. Called on
        every read/action, so keep it cheap."""
        ...
