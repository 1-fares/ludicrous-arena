"""Small shared helpers for game authors.

``merge_defaults`` is the one piece of plumbing every game repeated: pull the
defaults out of ``GameMeta.config_schema`` and overlay the caller's config,
ignoring ``_``-prefixed keys (reserved for engine-internal use). Centralizing it
documents the underscore convention in one place instead of in every game.
"""

from __future__ import annotations

from typing import Any

from arena.models import GameMeta


def merge_defaults(meta: GameMeta, config: dict[str, Any]) -> dict[str, Any]:
    props = meta.config_schema.get("properties", {})
    defaults = {k: v.get("default") for k, v in props.items()}
    return {**defaults, **{k: v for k, v in config.items() if not k.startswith("_")}}


def validate_config(meta: GameMeta, config: dict[str, Any]) -> None:
    """Type- and range-check a caller's config against the game's config_schema.
    Raises ValueError on the first violation. This is the guard that stops a
    malformed config (e.g. an out-of-range maze density) from ever reaching game
    code, where it could hang or crash a worker. Unknown and ``_``-prefixed keys
    are ignored (games tolerate extra keys)."""
    props = meta.config_schema.get("properties", {})
    for k, v in config.items():
        if k.startswith("_") or k not in props:
            continue
        spec = props[k]
        t = spec.get("type")
        if t in ("integer", "number"):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or (t == "integer" and not isinstance(v, int)):
                raise ValueError(f"config.{k} must be {'an integer' if t == 'integer' else 'a number'}")
            lo, hi, xhi = spec.get("minimum"), spec.get("maximum"), spec.get("exclusiveMaximum")
            if lo is not None and v < lo:
                raise ValueError(f"config.{k} must be >= {lo}")
            if hi is not None and v > hi:
                raise ValueError(f"config.{k} must be <= {hi}")
            if xhi is not None and v >= xhi:
                raise ValueError(f"config.{k} must be < {xhi}")
        elif t == "boolean" and not isinstance(v, bool):
            raise ValueError(f"config.{k} must be a boolean")
        elif t == "string" and not isinstance(v, str):
            raise ValueError(f"config.{k} must be a string")
