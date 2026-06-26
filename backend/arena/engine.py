"""The simulation, evaluated on demand.

There is no server loop and nothing lives in memory between requests. The
authoritative state of a match sits in DynamoDB; the engine advances it lazily:

- On a **read** (agent observation or spectator scene) the engine loads the
  persisted state, fast-forwards the deterministic simulation to *now* in memory,
  and returns the projection without writing anything.
- On an **action** the engine loads the state, fast-forwards to now, applies the
  action, and writes the result back under an optimistic version check (retrying
  if a concurrent agent got there first).

Because games are deterministic, "the world at time T" is a pure function of the
last persisted state and the elapsed wall-clock time, so a read computing it in
memory and an action persisting it agree. Catch-up stops as soon as ``result``
fires, so an idle match cannot run away (its time limit bounds the work).

Note on finished matches: once a match ends, META holds the terminal result and
STATE is no longer advanced. Reads still recompute ``result`` from STATE, but
``_advance`` always stops at the *first* tick the win condition holds, so every
projection from the same STATE reaches the identical ``finished_tick`` -- META's
stored result and any recomputation agree. STATE.last_tick lagging META is
therefore harmless; STATE is the durable seed, not a second source of truth.

This is what lets the whole service be Lambda + DynamoDB: zero cost when nobody is
playing, cost proportional to actual reads and actions when they are.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Callable, Optional

from arena import registry
from arena.config import merge_defaults, validate_config
from arena.game import Game
from arena.models import MatchInfo, MatchPhase, MatchResult, PlayerSlot
from arena.store import Conflict, StateRecord, Store

_MAX_CATCHUP = 100_000  # safety valve: never fast-forward more ticks than this in one call
_MAX_ACTIONS = 256      # per-submission action cap, bounds per-request work
_RETRY = 6              # optimistic-write attempts before giving up


class NotParticipant(Exception):
    """The caller holds a valid token but is not a player in this match."""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Engine:
    def __init__(self, store: Store, clock: Optional[Callable[[], float]] = None) -> None:
        self._store = store
        self._clock = clock or time.time  # injectable for deterministic tests

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    # -- meta loading ------------------------------------------------------

    def _load_info(self, match_id: str) -> tuple[MatchInfo, int]:
        """Load + validate match metadata, or raise KeyError. Single place that
        turns a stored dict into a (MatchInfo, version) pair."""
        meta = self._store.get_match_meta(match_id)
        if meta is None:
            raise KeyError(f"unknown match: {match_id}")
        return MatchInfo.model_validate(meta[0]), meta[1]

    def get_info(self, match_id: str) -> MatchInfo:
        return self._load_info(match_id)[0]

    # -- lifecycle ---------------------------------------------------------

    def create_match(self, game_id: str, config: dict[str, Any], autostart: bool,
                     room: Optional[str] = None) -> MatchInfo:
        game = registry.get(game_id)
        validate_config(game.meta, config)  # reject out-of-range config before it can hang/crash a start
        # Store the fully resolved config (defaults merged with the overrides), not
        # just the overridden keys, so an agent can read the real grid/fire_range/etc
        # off the match object instead of guessing the schema defaults. init_state
        # merges defaults again, which is idempotent on an already-resolved config.
        config = merge_defaults(game.meta, config)
        if room:
            # Named room: a stable, deterministic id reused across a whole session.
            match_id = "room-" + hashlib.sha256(room.encode()).hexdigest()[:10]
            existing = self._store.get_match_meta(match_id)
            if existing is not None:
                info = MatchInfo.model_validate(existing[0])
                if info.phase == MatchPhase.finished:
                    return self.reset_match(match_id)   # reuse the room, fresh round 1
                return info                              # lobby/running: hand it back
        else:
            match_id = uuid.uuid4().hex[:12]
        info = MatchInfo(
            match_id=match_id, game_id=game_id, phase=MatchPhase.lobby,
            config=config, autostart=autostart, players=[], room=room,
            max_players=game.meta.max_players, created_at=_now_iso(),
        )
        self._store.put_match_meta(match_id, info.model_dump(mode="json"), expected_version=None)
        self._store.update_match_index(match_id, game_id, MatchPhase.lobby.value)
        # Games that support add_player get their board built now (no players yet), so
        # the real maze and joined characters are visible in the lobby before start.
        if hasattr(game, "add_player"):
            state = game.init_state(config, [])
            self._store.put_match_state(match_id, StateRecord(
                state=game.encode_state(state), last_tick=0, last_wall_ms=self._now_ms()),
                expected_version=None)
        return info

    def join_match(self, match_id: str, user_id: str, display_name: str,
                   team: Optional[str]) -> MatchInfo:
        for _ in range(_RETRY):
            info, version = self._load_info(match_id)
            game = registry.get(info.game_id)
            # Idempotent: if you already hold a slot, resume it, even mid-match.
            # This lets an agent whose process died re-attach to its seat instead
            # of being locked out with a 409.
            existing = next((p for p in info.players if p.user_id == user_id), None)
            if existing:
                return info
            if len(info.players) >= game.meta.max_players:
                raise ValueError("match full")
            teams = game.meta.teams
            assigned = team if team in teams else (
                teams[len(info.players) % len(teams)] if teams else None)
            # Keep display names unique within the match: per-name scoreboards
            # (which some games key by name) would otherwise collide.
            taken = {p.display_name for p in info.players}
            name, n = display_name, 2
            while name in taken:
                name, n = f"{display_name} ({n})", n + 1
            slot = PlayerSlot(player_id=f"p{len(info.players) + 1}", user_id=user_id,
                              display_name=name, team=assigned)
            info.players.append(slot)
            # Record the user as a participant for good. Unlike `players` (which the
            # roster prune trims when a client drops), this list is never pruned, so
            # a finished-match read can still recognise a past participant. Guard
            # against a re-join after a prune adding the same user twice.
            if user_id not in info.participants:
                info.participants.append(user_id)
            # Put the player on the board at score 0 (the game decides how, via
            # add_player), whether the match is in the lobby (so they appear in the
            # waiting arena) or already running (a late join). A join while finished
            # just enters the roster and the next reset includes them.
            if info.phase in (MatchPhase.lobby, MatchPhase.running) and hasattr(game, "add_player"):
                loaded = self._store.get_match_state(match_id)
                if loaded is not None:
                    rec, sver = loaded
                    state = game.decode_state(rec.state)
                    game.add_player(state, slot)  # idempotent on player_id
                    try:
                        self._store.put_match_state(match_id, StateRecord(
                            state=game.encode_state(state), last_tick=rec.last_tick,
                            last_wall_ms=rec.last_wall_ms, rejected=rec.rejected), sver)
                    except Conflict:
                        continue  # state advanced; reload roster + state and retry
            try:
                self._store.put_match_meta(match_id, info.model_dump(mode="json"), version)
            except Conflict:
                continue  # someone else joined concurrently; reload and retry
            if info.phase == MatchPhase.lobby and info.autostart and len(info.players) >= game.meta.min_players:
                # Best-effort: the join already committed, so a start race must not
                # surface as a join error. An explicit POST /start can recover.
                try:
                    self.start_match(match_id)
                except (ValueError, Conflict):
                    pass
                return self.get_info(match_id)
            return info
        raise ValueError("join contention; retry")

    def start_match(self, match_id: str) -> None:
        info, version = self._load_info(match_id)
        game = registry.get(info.game_id)
        if info.phase != MatchPhase.lobby:
            return
        if len(info.players) < game.meta.min_players:
            raise ValueError("not enough players")
        loaded = self._store.get_match_state(match_id)
        if loaded is None:
            # No pre-built board (games without add_player): build it now with the roster.
            state = game.init_state(info.config, info.players)
            rec = StateRecord(state=game.encode_state(state), last_tick=0, last_wall_ms=self._now_ms())
            sver = None
        else:
            # The lobby board already holds the maze and the joined players: just
            # start the clock, do not rebuild (that would reshuffle the maze/spawns).
            rec0, sver = loaded
            rec = StateRecord(state=rec0.state, last_tick=0, last_wall_ms=self._now_ms())
        try:
            self._store.put_match_state(match_id, rec, sver)
            info.phase = MatchPhase.running
            self._store.put_match_meta(match_id, info.model_dump(mode="json"), version)
            self._store.update_match_index(match_id, info.game_id, MatchPhase.running.value)
        except Conflict:
            return  # a concurrent start won the race; the match is already starting

    def reset_match(self, match_id: str) -> MatchInfo:
        """Wipe a match's world back to a fresh start, keeping the same id and the
        same roster. Scores, rounds, and positions reset; phase returns to running.
        Agents still polling the same match just see round 1 and keep playing, no
        re-join needed. Other matches are untouched. Admin-gated at the API.

        Uses the optimistic version check like every other write: a fresh STATE is
        written over the current one, so an agent's in-flight action loses its
        version race, reloads, and continues from the reset world."""
        for _ in range(_RETRY):
            info, mver = self._load_info(match_id)
            game = registry.get(info.game_id)
            if len(info.players) < game.meta.min_players:
                raise ValueError("not enough players to reset")
            state = game.init_state(info.config, info.players)
            rec = StateRecord(state=game.encode_state(state), last_tick=0, last_wall_ms=self._now_ms())
            loaded = self._store.get_match_state(match_id)
            state_ver = loaded[1] if loaded else None
            info.phase = MatchPhase.running
            info.result = None
            info.tick = 0
            info.generation += 1   # signal the reset to agents polling the same id
            info.break_until = None
            info.break_note = None
            try:
                self._store.put_match_state(match_id, rec, state_ver)
                self._store.put_match_meta(match_id, info.model_dump(mode="json"), mver)
            except Conflict:
                continue  # a concurrent action/start moved a version; reload and retry
            self._store.update_match_index(match_id, info.game_id, MatchPhase.running.value)
            return self.get_info(match_id)
        raise ValueError("reset contention; retry")

    def delete_match(self, match_id: str) -> None:
        """Admin: remove a match entirely (meta, state, result, index). Raises
        KeyError if it does not exist so the API can 404."""
        self._load_info(match_id)  # raises KeyError on unknown match
        self._store.delete_match(match_id)

    def set_break(self, match_id: str, break_until: Optional[float], note: Optional[str]) -> MatchInfo:
        """Admin: record an intermission window on a (finished) match so agents and
        the viewer can show a countdown and a note. Does not change phase; the next
        round begins with reset_match when the break ends."""
        for _ in range(_RETRY):
            info, version = self._load_info(match_id)
            info.break_until = break_until
            info.break_note = note
            try:
                self._store.put_match_meta(match_id, info.model_dump(mode="json"), version)
            except Conflict:
                continue
            return self.get_info(match_id)
        raise ValueError("break contention; retry")

    # -- simulation core ---------------------------------------------------

    def _target_tick(self, game: Game, rec: StateRecord) -> int:
        """Tick the world should be at right now, given the wall clock."""
        if not game.meta.realtime:
            return rec.last_tick
        elapsed_s = max(0, self._now_ms() - rec.last_wall_ms) / 1000.0
        return rec.last_tick + int(elapsed_s * game.meta.tick_rate)

    def _advance(self, game: Game, state: Any, from_tick: int, to_tick: int):
        """Fast-forward ``state`` from ``from_tick`` to ``to_tick`` (or until the
        game ends). Returns (tick_reached, result_or_None)."""
        dt = 1.0 / game.meta.tick_rate
        steps = min(max(0, to_tick - from_tick), _MAX_CATCHUP)
        tick = from_tick
        result = game.result(state)
        for _ in range(steps):
            if result is not None:
                break
            game.tick(state, dt)
            tick += 1
            result = game.result(state)
        return tick, result

    def _project(self, match_id: str):
        """Read-only: load meta + state, fast-forward to now in memory. Returns
        (game, info, version_meta, state, tick, result, rec, version). No write."""
        info, mver = self._load_info(match_id)
        game = registry.get(info.game_id)
        loaded = self._store.get_match_state(match_id)
        if loaded is None:
            return game, info, None, 0, None, None, None
        rec, version = loaded
        state = game.decode_state(rec.state)
        if info.phase == MatchPhase.lobby:
            # The lobby board is a static preview; the simulation has not begun.
            return game, info, state, rec.last_tick, None, rec, version
        if info.phase == MatchPhase.finished:
            # The match is over. Return the stored terminal result and the frozen
            # state without re-simulating: a fresh projection from the persisted
            # seed (which excludes the actions that actually decided the game) could
            # otherwise reach a different winner than the one in /v1/matches. The
            # stored result is the single source of truth once finished.
            return game, info, state, info.tick, info.result, rec, version
        tick, result = self._advance(game, state, rec.last_tick, self._target_tick(game, rec))
        return game, info, state, tick, result, rec, version

    # -- reads -------------------------------------------------------------

    def agent_view(self, match_id: str, user_id: str) -> dict[str, Any]:
        """Calling agent's partial observation, projected to now. Resolves the
        caller's player slot from the metadata it already loaded (one round trip)."""
        game, info, state, tick, result, rec, _ = self._project(match_id)
        slot = next((p for p in info.players if p.user_id == user_id), None)
        if slot is None:
            # Finished match: a past participant whose live slot was pruned (e.g. a
            # dropped client removed from the roster) still gets the frozen result
            # rather than a 403. Their slot and game presence are gone, so the seat
            # and observation are minimal stubs; the durable result is what matters.
            # Running matches keep the strict check: only the live roster may read.
            if info.phase == MatchPhase.finished and user_id in info.participants:
                return {
                    "match_id": match_id,
                    "tick": info.tick,
                    "phase": MatchPhase.finished.value,
                    "seat": {"player_id": "", "team": None, "rejected": []},
                    "observation": {"phase": MatchPhase.finished.value},
                    "result": (info.result.model_dump() if info.result else None),
                }
            raise NotParticipant("not a participant in this match")
        if state is None or info.phase == MatchPhase.lobby:
            raise ValueError("match has not started")
        self._lazy_finalize(match_id, result, tick)
        return {
            "match_id": match_id,
            "tick": tick,
            "phase": (MatchPhase.finished if result else info.phase).value,
            "seat": {
                "player_id": slot.player_id,
                "team": slot.team,
                "rejected": (rec.rejected.get(slot.player_id, []) if rec else []),
            },
            "observation": game.observe(state, slot.player_id),
            "result": (result.model_dump() if result else None),
        }

    def scene_view(self, match_id: str) -> dict[str, Any]:
        """Full spectator render. Public (no auth); spectating is omniscient."""
        game, info, state, tick, result, _, _ = self._project(match_id)
        if state is None:
            return {"match_id": match_id, "tick": 0, "phase": info.phase.value, "scene": None}
        self._lazy_finalize(match_id, result, tick)
        return {
            "match_id": match_id,
            "tick": tick,
            "phase": (MatchPhase.finished if result else info.phase).value,
            "scene": game.render(state),
            "result": (result.model_dump() if result else None),
        }

    # -- actions -----------------------------------------------------------

    def submit_actions(self, match_id: str, user_id: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply a batch of actions, then return the caller's state envelope as of
        after the apply: the same shape as ``agent_view`` ({match_id, tick, phase,
        seat, observation, result}). This lets a client run one round trip per tick
        (a single POST) instead of POST-then-GET. ``seat.rejected`` here reflects
        *this* submission, not the prior tick."""
        if len(actions) > _MAX_ACTIONS:
            raise ValueError(f"too many actions in one submission (max {_MAX_ACTIONS})")
        info, _ = self._load_info(match_id)
        game = registry.get(info.game_id)
        slot = next((p for p in info.players if p.user_id == user_id), None)
        if slot is None:
            raise NotParticipant("not a participant in this match")
        if info.phase == MatchPhase.lobby:
            raise ValueError("match has not started")
        player_id = slot.player_id
        rate = game.meta.tick_rate

        for _ in range(_RETRY):
            loaded = self._store.get_match_state(match_id)
            if loaded is None:
                raise ValueError("match has not started")
            rec, version = loaded
            state = game.decode_state(rec.state)
            tick, result = self._advance(game, state, rec.last_tick, self._target_tick(game, rec))
            if result is not None:
                self._lazy_finalize(match_id, result, tick)
                raise ValueError("match finished")
            rejected: list[str] = []
            for action in actions:
                err = game.validate(state, player_id, action)
                if err is None:
                    game.apply(state, player_id, action)
                else:
                    rejected.append(err)
            # Turn-paced games advance exactly one tick per submission; realtime
            # games let the wall clock advance them on the next read/action.
            if not game.meta.realtime:
                game.tick(state, 1.0 / rate)
                tick += 1
                new_wall = self._now_ms()
            else:
                # Carry the wall clock forward by exactly the ticks consumed, not
                # to raw "now", so the dropped sub-tick remainder does not accrue
                # as drift across many short submissions.
                new_wall = rec.last_wall_ms + round((tick - rec.last_tick) * 1000.0 / rate)
            result = game.result(state)
            # Merge this player's rejections into the existing map so a concurrent
            # player's feedback is not clobbered before they read it.
            merged = {**rec.rejected, player_id: rejected}
            new_rec = StateRecord(state=game.encode_state(state), last_tick=tick,
                                  last_wall_ms=new_wall, rejected=merged)
            try:
                self._store.put_match_state(match_id, new_rec, version)
            except Conflict:
                continue  # a concurrent agent wrote first; reload and reapply
            self._prune_roster(match_id, game, state)  # drop players the game removed
            if result is not None:
                self._lazy_finalize(match_id, result, tick)
            # Return the post-apply envelope so the caller needs no follow-up read.
            return {
                "match_id": match_id,
                "tick": tick,
                "phase": (MatchPhase.finished if result else info.phase).value,
                "seat": {
                    "player_id": slot.player_id,
                    "team": slot.team,
                    "rejected": rejected,
                },
                "observation": game.observe(state, player_id),
                "result": (result.model_dump() if result else None),
            }
        raise ValueError("write contention; retry")

    def _prune_roster(self, match_id: str, game: Game, state: Any) -> None:
        """Remove from the match roster any player the game has dropped (e.g. a gone
        client). Best-effort: keeps MatchInfo.players in step with who is actually in
        the world, so the viewer and the lobby reflect reality. Games that do not
        implement active_players keep their full roster."""
        active = getattr(game, "active_players", None)
        if active is None:
            return
        ids = active(state)
        loaded = self._store.get_match_meta(match_id)
        if loaded is None:
            return
        info = MatchInfo.model_validate(loaded[0])
        kept = [p for p in info.players if p.player_id in ids]
        if len(kept) == len(info.players):
            return
        info.players = kept
        try:
            self._store.put_match_meta(match_id, info.model_dump(mode="json"), loaded[1])
        except Conflict:
            pass  # another writer updated meta; the next prune catches any remainder

    # -- helpers -----------------------------------------------------------

    def _lazy_finalize(self, match_id: str, result: Optional[MatchResult], tick: int) -> None:
        """Persist the terminal record the first time anyone observes the end.
        Best-effort: a lost version race just means another caller finalized.
        ``result`` is deterministic (first tick the win condition holds), so the
        first writer records the canonical outcome."""
        if result is None:
            return
        loaded = self._store.get_match_meta(match_id)
        if loaded is None:
            return
        meta = MatchInfo.model_validate(loaded[0])
        if meta.phase == MatchPhase.finished:
            return
        # Fill in the winners' display names from the roster so a client can map a
        # winning player_id to a name without a second lookup. Mutating in place also
        # gives the calling read/action the enriched result for its own response.
        if not result.winner_names:
            by_pid = {p.player_id: p.display_name for p in meta.players}
            result.winner_names = [by_pid.get(w, w) for w in result.winners]
        meta.phase = MatchPhase.finished
        meta.tick = tick
        meta.result = result
        try:
            self._store.put_match_meta(match_id, meta.model_dump(mode="json"), loaded[1])
            self._store.update_match_index(match_id, meta.game_id, MatchPhase.finished.value)
            self._store.save_match_result(match_id, meta.game_id, result.model_dump())
        except Conflict:
            pass

    def list_matches(self, phases: Optional[set[str]] = None,
                     game_id: Optional[str] = None) -> list[MatchInfo]:
        """All matches, optionally filtered. The cheap INDEX rows carry phase and
        game_id, so filtering avoids loading the meta of matches we will drop."""
        out = []
        for entry in self._store.list_match_index():
            if phases is not None and entry.get("phase") not in phases:
                continue
            if game_id is not None and entry.get("game_id") != game_id:
                continue
            loaded = self._store.get_match_meta(entry["match_id"])
            if loaded:
                out.append(MatchInfo.model_validate(loaded[0]))
        return out
