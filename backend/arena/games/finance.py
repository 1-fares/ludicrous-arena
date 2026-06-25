"""Trading Desk -- the first industry-scenario game (mode `scenario`).

Each agent runs the same desk over an identical, deterministic price path: same
seed, same prices, same starting cash. They trade through a fixed horizon and are
ranked by final equity (cash + mark-to-market position). Because every agent faces
the *identical* market, the contest is pure skill, and because each agent advances
its own step through that path, there is no shared-order-matching to coordinate.
This is the "isolated-instance" scenario shape: competitive, deterministic, and a
clean fit for the on-demand engine.

It is turn-paced (`realtime=false`): the world advances exactly one step each time
an agent submits, so there is no wall clock and no time pressure, just decisions.
The challenge is partial information: an agent sees the price history up to its own
step but never the future.

Actions (one per submission):
    {"type": "trade", "delta": <int>}   buy (delta>0) or sell/short (delta<0) at the
                                        current price, then advance one step
    {"type": "hold"}                    advance one step without trading

Config (all optional):
    seed:         int   = 1       deterministic market seed
    horizon:      int   = 40      steps each agent trades through
    start_cash:   float = 10000   starting cash
    start_price:  float = 100     price at step 0
    drift:        float = 0.0005  per-step expected return
    volatility:   float = 0.02    per-step return stdev
    max_position: int   = 100     position bound, [-max_position, +max_position]
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from arena.config import merge_defaults
from arena.models import GameMeta, GameMode, MatchResult, PlayerSlot
from arena.registry import register

META = GameMeta(
    id="trading_desk",
    title="Trading Desk",
    description="Industry scenario (financial services): every agent trades the same deterministic price path from the same starting cash and is ranked by final equity. Turn-paced; the future is hidden.",
    mode=GameMode.scenario,
    min_players=1,
    max_players=8,
    teams=[],
    tick_rate=1.0,
    realtime=False,  # turn-paced: one step per submission, no wall clock
    config_schema={
        "type": "object",
        "properties": {
            "seed": {"type": "integer", "default": 1},
            "horizon": {"type": "integer", "default": 40},
            "start_cash": {"type": "number", "default": 10000},
            "start_price": {"type": "number", "default": 100},
            "drift": {"type": "number", "default": 0.0005},
            "volatility": {"type": "number", "default": 0.02},
            "max_position": {"type": "integer", "default": 100},
        },
    },
    action_schema={
        "oneOf": [
            {"type": "object", "required": ["type", "delta"],
             "properties": {"type": {"const": "trade"}, "delta": {"type": "integer"}}},
            {"type": "object", "required": ["type"],
             "properties": {"type": {"const": "hold"}}},
        ]
    },
    observation_schema={
        "type": "object",
        "description": "Partial view: prices only up to your own step; the future is hidden.",
        "properties": {
            "asset": {"type": "string"},
            "horizon": {"type": "integer"},
            "max_position": {"type": "integer"},
            "step": {"type": "integer", "description": "Your index into the price path."},
            "price": {"type": "number", "description": "Current price at your step."},
            "price_history": {"type": "array", "items": {"type": "number"},
                              "description": "Prices from step 0 up to your current step."},
            "you": {"type": "object", "properties": {
                "cash": {"type": "number"}, "position": {"type": "integer"},
                "equity": {"type": "number", "description": "cash + position * price."},
                "done": {"type": "boolean"}}},
            "players": {"type": "integer", "description": "Number of competitors."},
        },
    },
)


@dataclass
class Trader:
    name: str
    cash: float
    position: int = 0
    step: int = 0  # index into the price path; advances one per submission


@dataclass
class State:
    cfg: dict[str, Any]
    prices: list[float]              # deterministic path, length horizon + 1
    traders: dict[str, Trader]
    tick: int = 0
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)


def _make_prices(cfg: dict[str, Any]) -> list[float]:
    """Deterministic geometric random walk from the seed. Computed once at init
    and stored, so no randomness runs at tick time (the engine fast-forwards
    `tick`, which must stay pure)."""
    rng = random.Random(cfg["seed"])
    price = float(cfg["start_price"])
    out = [round(price, 2)]
    for _ in range(int(cfg["horizon"])):
        price *= 1.0 + cfg["drift"] + cfg["volatility"] * rng.gauss(0.0, 1.0)
        price = max(0.01, price)  # a price floor keeps equity finite
        out.append(round(price, 2))
    return out


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class TradingDesk:
    meta = META

    def init_state(self, config: dict[str, Any], players: list[PlayerSlot]) -> State:
        cfg = merge_defaults(self.meta, config)
        prices = _make_prices(cfg)
        traders = {p.player_id: Trader(name=p.display_name, cash=float(cfg["start_cash"]))
                   for p in players}
        return State(cfg=cfg, prices=prices, traders=traders)

    # -- helpers -----------------------------------------------------------

    def _price_at(self, state: State, step: int) -> float:
        return state.prices[min(step, len(state.prices) - 1)]

    def _equity(self, state: State, t: Trader) -> float:
        return round(t.cash + t.position * self._price_at(state, t.step), 2)

    def _done(self, state: State, t: Trader) -> bool:
        return t.step >= state.cfg["horizon"]

    # -- rules -------------------------------------------------------------

    def validate(self, state: State, player_id: str, action: dict[str, Any]) -> Optional[str]:
        t = action.get("type")
        trader = state.traders[player_id]
        if self._done(state, trader):
            return "your horizon is reached; no more trades"
        if t == "hold":
            return None
        if t != "trade":
            return f"unknown action type: {t!r}"
        delta = action.get("delta")
        if not _int(delta):
            return "trade requires an integer delta"
        new_pos = trader.position + delta
        cap = state.cfg["max_position"]
        if abs(new_pos) > cap:
            return f"position {new_pos} exceeds limit +/-{cap}"
        price = self._price_at(state, trader.step)
        if delta > 0 and trader.cash - delta * price < 0:
            return "insufficient cash for this buy"
        return None

    def apply(self, state: State, player_id: str, action: dict[str, Any]) -> None:
        # Record intent; resolution happens in tick so a turn is atomic.
        state.pending[player_id] = action

    def tick(self, state: State, dt: float) -> None:
        for pid, action in state.pending.items():
            trader = state.traders[pid]
            if self._done(state, trader):
                continue
            if action.get("type") == "trade":
                delta = int(action["delta"])
                price = self._price_at(state, trader.step)
                trader.cash -= delta * price
                trader.position += delta
            trader.step += 1  # advance to the next price; the future was hidden
        state.pending = {}
        state.tick += 1

    def observe(self, state: State, player_id: str) -> dict[str, Any]:
        t = state.traders[player_id]
        # Partial information: the agent sees prices only up to its own step.
        return {
            "asset": "ACME",
            "horizon": state.cfg["horizon"],
            "max_position": state.cfg["max_position"],
            "step": t.step,
            "price": self._price_at(state, t.step),
            "price_history": state.prices[: t.step + 1],
            "you": {"cash": round(t.cash, 2), "position": t.position,
                    "equity": self._equity(state, t), "done": self._done(state, t)},
            "players": len(state.traders),
        }

    def render(self, state: State) -> dict[str, Any]:
        max_step = max((t.step for t in state.traders.values()), default=0)
        return {
            "asset": "ACME",
            "horizon": state.cfg["horizon"],
            "start_cash": state.cfg["start_cash"],
            "prices": state.prices[: max_step + 1],  # spectators see the revealed path
            "players": [
                {"id": pid, "name": t.name, "cash": round(t.cash, 2), "position": t.position,
                 "equity": self._equity(state, t), "step": t.step, "done": self._done(state, t)}
                for pid, t in state.traders.items()
            ],
        }

    def result(self, state: State) -> Optional[MatchResult]:
        traders = state.traders
        equities = {pid: self._equity(state, t) for pid, t in traders.items()}
        if all(self._done(state, t) for t in traders.values()):
            top = max(equities.values())
            return MatchResult(finished_tick=state.tick,
                               winners=[pid for pid, e in equities.items() if e == top],
                               scores=equities, reason="horizon reached")
        # Safety cap so a stalled player cannot keep the match open forever (no
        # wall clock in turn-paced mode). Generous: every player could trade fully.
        if state.tick >= state.cfg["horizon"] * max(1, len(traders)) * 4:
            top = max(equities.values())
            return MatchResult(finished_tick=state.tick,
                               winners=[pid for pid, e in equities.items() if e == top],
                               scores=equities, reason="step cap reached")
        return None

    # -- persistence -------------------------------------------------------

    def encode_state(self, state: State) -> dict[str, Any]:
        return {
            "cfg": state.cfg,
            "prices": state.prices,
            "traders": {pid: asdict(t) for pid, t in state.traders.items()},
            "tick": state.tick,
            "pending": state.pending,
        }

    def decode_state(self, data: dict[str, Any]) -> State:
        return State(
            cfg=data["cfg"],
            prices=data["prices"],
            traders={pid: Trader(**t) for pid, t in data["traders"].items()},
            tick=data["tick"],
            pending=data["pending"],
        )


register(TradingDesk())
