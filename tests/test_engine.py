"""Engine-level tests: the store-backed, lazily-evaluated simulation. These drive
the real Engine against a MemoryStore with a controllable clock, so wall-clock
catch-up, optimistic writes, and lazy finalization are all deterministic.
"""

import arena.games  # noqa: F401  -- registers games
from arena.engine import Engine, NotParticipant, _now_iso
from arena.models import MatchInfo, MatchPhase
from arena.store import MemoryStore


def _engine():
    now = [1_000.0]
    eng = Engine(MemoryStore(), clock=lambda: now[0])
    return eng, now


def _start_deathmatch(eng, **cfg):
    info = eng.create_match("deathmatch", {"score_limit": 1, "arena_size": 12, **cfg}, autostart=True)
    mid = info.match_id
    eng.join_match(mid, "u1", "A", None)
    info = eng.join_match(mid, "u2", "B", None)  # autostarts at min_players=2
    assert info.phase.value == "running"
    return mid


def test_wall_clock_catch_up():
    eng, now = _engine()
    mid = _start_deathmatch(eng)
    # No time has passed: tick 0.
    assert eng.scene_view(mid)["tick"] == 0
    # Advance the clock 1.0s at 10 tps -> the projection is 10 ticks ahead, with
    # no write needed (read-only forward projection).
    now[0] += 1.0
    assert eng.scene_view(mid)["tick"] == 10


def test_kill_scores_and_finishes():
    eng, now = _engine()
    mid = _start_deathmatch(eng)  # score_limit 1

    # Hand-place the duel: p1 at (5,5), p2 one unit away at 20 HP, so a single
    # projectile is lethal. Edit the persisted state directly.
    rec, ver = eng._store.get_match_state(mid)
    rec.state["entities"]["p1"].update({"x": 5.0, "y": 5.0})
    rec.state["entities"]["p2"].update({"x": 6.0, "y": 5.0, "hp": 20})
    eng._store.put_match_state(mid, rec, ver)

    # p1 fires straight along +x. The projectile spawns this tick.
    eng.submit_actions(mid, "u1", [{"type": "fire", "angle": 0.0}])

    # Let a few ticks elapse; the projectile crosses the gap and kills p2, which
    # takes p1 to the score limit and ends the match.
    now[0] += 0.3
    scene = eng.scene_view(mid)
    assert scene["phase"] == "finished"
    assert scene["result"]["winners"] == ["p1"]
    # The result carries the winner's display name, so a client need not map ids.
    assert scene["result"]["winner_names"] == ["A"]

    # The terminal record was persisted (lazy finalize), so metadata agrees and a
    # further action is rejected.
    info = eng.get_info(mid)
    assert info.phase.value == "finished"
    # Reads of a finished match return the stored result verbatim and do not advance
    # the sim, so /state and /v1/matches cannot disagree on the winner even as the
    # clock keeps moving.
    assert info.result.winners == ["p1"] and info.result.winner_names == ["A"]
    now[0] += 50.0
    assert eng.scene_view(mid)["result"]["winners"] == info.result.winners
    assert eng.agent_view(mid, "u1")["result"]["winners"] == info.result.winners
    assert eng.scene_view(mid)["tick"] == info.tick  # frozen, not re-projected
    # Regression: the win was reached by a read projecting forward (the kill happened
    # after the last action persisted state). The finishing scene must be persisted at
    # finalize, so a later frozen read shows that final world, not the pre-kill state.
    assert eng.scene_view(mid)["scene"] == scene["scene"]
    try:
        eng.submit_actions(mid, "u2", [{"type": "move", "dx": 1, "dy": 0}])
        assert False, "expected finished match to reject actions"
    except ValueError:
        pass


def test_non_finite_action_is_rejected_not_crashing():
    eng, now = _engine()
    mid = _start_deathmatch(eng, score_limit=5)
    now[0] += 0.1
    # A hostile/buggy agent sending angle=Infinity must NOT crash the tick; it is
    # rejected and the reason surfaces on the agent's next observation.
    eng.submit_actions(mid, "u1", [{"type": "fire", "angle": float("inf")}])
    view = eng.agent_view(mid, "u1")
    assert any("finite" in r for r in view["seat"]["rejected"])


def test_rejected_feedback_not_clobbered_across_players():
    eng, now = _engine()
    mid = _start_deathmatch(eng, score_limit=5)
    now[0] += 0.1
    eng.submit_actions(mid, "u1", [{"type": "bogus"}])   # rejected for p1
    now[0] += 0.1
    eng.submit_actions(mid, "u2", [{"type": "bogus"}])   # must not wipe p1's feedback
    assert eng.agent_view(mid, "u1")["seat"]["rejected"]
    assert eng.agent_view(mid, "u2")["seat"]["rejected"]


def test_config_has_no_internal_autostart_key():
    eng, _ = _engine()
    info = eng.create_match("deathmatch", {"score_limit": 3}, autostart=True)
    assert "_autostart" not in info.config
    assert info.autostart is True


def test_create_resolves_full_config():
    # The match object surfaces the resolved config (defaults merged), not just the
    # caller's overrides, so an agent reads real values instead of guessing.
    eng, _ = _engine()
    info = eng.create_match("skirmish", {"grid": 15}, autostart=False)
    assert info.config["grid"] == 15            # the override
    assert info.config["hearts"] == 3           # a default now made visible
    assert info.config["fire_range"] == 4


def test_reset_increments_generation():
    # Reusing a room id across resets bumps a generation counter so a polling agent
    # can tell a fresh game from a continued one (same id, higher generation).
    eng, _ = _engine()
    mid = _start_deathmatch(eng)
    assert eng.get_info(mid).generation == 0
    eng.reset_match(mid)
    assert eng.get_info(mid).generation == 1
    eng.reset_match(mid)
    assert eng.get_info(mid).generation == 2


def test_trading_desk_turn_paced_through_engine():
    eng, now = _engine()
    info = eng.create_match("trading_desk",
                            {"seed": 1, "horizon": 3, "start_cash": 1000,
                             "start_price": 100, "drift": 0, "volatility": 0},
                            autostart=True)
    mid = info.match_id
    eng.join_match(mid, "u1", "A", None)  # min_players 1 -> autostarts
    assert eng.get_info(mid).phase.value == "running"
    # Turn-paced: each submission advances exactly one step, independent of the
    # wall clock (the clock does not move between these calls).
    eng.submit_actions(mid, "u1", [{"type": "trade", "delta": 3}])
    eng.submit_actions(mid, "u1", [{"type": "hold"}])
    eng.submit_actions(mid, "u1", [{"type": "hold"}])
    info = eng.get_info(mid)
    assert info.phase.value == "finished"
    assert info.result.scores["p1"] == 1000.0  # flat market -> no P&L


def test_optimistic_write_handles_two_agents_same_tick():
    eng, now = _engine()
    mid = _start_deathmatch(eng, score_limit=5)
    now[0] += 0.2
    # Two agents submit "at the same instant"; both must land without error and
    # the second must observe the first's effect (version retry, not a lost write).
    eng.submit_actions(mid, "u1", [{"type": "move", "dx": 1, "dy": 0}])
    eng.submit_actions(mid, "u2", [{"type": "move", "dx": -1, "dy": 0}])
    scene = eng.scene_view(mid)
    assert scene["phase"] == "running"
    assert len(scene["scene"]["players"]) == 2


def test_reset_match_wipes_world_keeps_roster():
    eng, now = _engine()
    info = eng.create_match("skirmish", {"grid": 11, "score_to_win": 10}, autostart=False)
    mid = info.match_id
    eng.join_match(mid, "u1", "Echo", None)
    eng.join_match(mid, "u2", "Fox", None)
    eng.start_match(mid)
    now[0] += 5.0                       # let the world tick on
    before = eng.scene_view(mid)
    assert before["tick"] > 0 and before["phase"] == "running"
    roster = [p.player_id for p in eng.get_info(mid).players]

    out = eng.reset_match(mid)
    assert out.phase.value == "running"
    assert [p.player_id for p in out.players] == roster   # same roster, same ids
    after = eng.scene_view(mid)
    assert after["tick"] == 0                             # world wiped to a fresh start
    assert after["scene"]["round"] == 1
    assert all(p["score"] == 0 for p in after["scene"]["players"])


def test_reset_keeps_agents_playing_after_finish():
    # A finished match can be reset back to running; an agent that kept its token
    # can submit again with no re-join.
    eng, now = _engine()
    mid = _start_deathmatch(eng)        # score_limit 1: ends on the first kill
    now[0] += 600.0
    eng.scene_view(mid)                 # observe -> lazy-finalize to finished
    assert eng.get_info(mid).phase.value == "finished"
    eng.reset_match(mid)
    assert eng.get_info(mid).phase.value == "running"
    now[0] += 0.2
    eng.submit_actions(mid, "u1", [{"type": "move", "dx": 1, "dy": 0}])  # plays again, no re-join


def test_late_join_into_running_match_starts_at_zero():
    # Flexibility: a player can join after the match has started; they appear at
    # score 0 even if others are ahead, and play from the current round.
    eng, now = _engine()
    info = eng.create_match("skirmish", {"grid": 13, "score_to_win": 10}, autostart=False)
    mid = info.match_id
    eng.join_match(mid, "u1", "Echo", None)
    eng.join_match(mid, "u2", "Fox", None)
    eng.start_match(mid)
    now[0] += 2.0
    out = eng.join_match(mid, "u3", "Gus", None)          # mid-game join
    assert out.phase.value == "running" and any(p.user_id == "u3" for p in out.players)
    scene = eng.scene_view(mid)["scene"]
    names = {p["name"]: p["score"] for p in scene["players"]}
    assert len(scene["players"]) == 3
    assert names.get("Gus") == 0.0                        # late joiner starts fresh


def test_dropped_player_is_pruned_from_roster():
    # A gone client is removed from the world and from the match roster, so the
    # lobby/viewer reflect who is actually playing (no phantom).
    eng, now = _engine()
    info = eng.create_match("skirmish", {"grid": 11, "drop_after": 3, "score_to_win": 10}, autostart=False)
    mid = info.match_id
    eng.join_match(mid, "u1", "A", None)
    eng.join_match(mid, "u2", "B", None)
    eng.start_match(mid)
    for _ in range(8):                        # u1 keeps acting; u2 never does
        now[0] += 0.1
        eng.submit_actions(mid, "u1", [{"type": "wait"}])
    roster = {p.user_id for p in eng.get_info(mid).players}
    assert "u1" in roster and "u2" not in roster


def test_seat_carries_own_display_name():
    # The agent is never sent its name except here: scoreboards are name-keyed, so the
    # envelope must tell each agent its own display name (both on read and on action).
    eng, now = _engine()
    mid = _start_deathmatch(eng, score_limit=5)
    now[0] += 0.1
    assert eng.agent_view(mid, "u1")["seat"]["name"] == "A"
    assert eng.agent_view(mid, "u2")["seat"]["name"] == "B"
    out = eng.submit_actions(mid, "u1", [{"type": "move", "dx": 1, "dy": 0}])
    assert out["seat"]["name"] == "A"


def test_submit_actions_returns_post_apply_view():
    # The action call returns the same envelope as a state read, computed after the
    # apply, so an agent needs one round trip per tick (submit + observe in one).
    eng, now = _engine()
    mid = _start_deathmatch(eng, score_limit=5)
    now[0] += 0.1
    out = eng.submit_actions(mid, "u1", [{"type": "move", "dx": 1, "dy": 0}])
    assert out["match_id"] == mid
    assert out["seat"]["player_id"] == "p1"
    assert out["phase"] == "running" and out["result"] is None
    assert "players" in out["observation"]


def test_submit_actions_view_surfaces_this_submission_rejections():
    # seat.rejected on the action response reflects THIS submission, not a prior tick.
    eng, now = _engine()
    mid = _start_deathmatch(eng, score_limit=5)
    now[0] += 0.1
    out = eng.submit_actions(mid, "u1", [{"type": "bogus"}])
    assert out["seat"]["rejected"]  # the bad action is reported back immediately


def test_finished_match_read_allows_pruned_participant():
    # Item J: a player who joined then was pruned (a dropped client removed from the
    # roster) can still read the FINISHED match and gets the frozen result, not a 403.
    eng, now = _engine()
    info = eng.create_match("skirmish", {"grid": 11, "drop_after": 3, "rounds_to_win": 1},
                            autostart=False)
    mid = info.match_id
    eng.join_match(mid, "u1", "A", None)
    eng.join_match(mid, "u2", "B", None)
    eng.start_match(mid)
    # u1 keeps acting; u2 never does, so u2 is dropped from the world and pruned from
    # the live roster, but stays recorded in participants.
    for _ in range(8):
        now[0] += 0.1
        eng.submit_actions(mid, "u1", [{"type": "wait"}])
    info = eng.get_info(mid)
    assert "u2" not in {p.user_id for p in info.players}
    assert "u2" in info.participants and "u1" in info.participants

    # Force a finish: give u1 the round-win limit in the persisted state, then a read
    # lazy-finalizes the match to finished.
    rec, ver = eng._store.get_match_state(mid)
    rec.state["fighters"]["p1"]["round_wins"] = 1
    eng._store.put_match_state(mid, rec, ver)
    now[0] += 0.1
    assert eng.scene_view(mid)["phase"] == "finished"
    assert eng.get_info(mid).phase.value == "finished"

    # The pruned participant reads the finished match: no NotParticipant, result present.
    view = eng.agent_view(mid, "u2")
    assert view["phase"] == "finished"
    assert view["result"]["winners"] == ["p1"]

    # A genuine non-participant is still rejected, even on a finished match.
    try:
        eng.agent_view(mid, "u-stranger")
        assert False, "expected a non-participant to be rejected"
    except NotParticipant:
        pass


def test_create_match_replaces_existing_single_arena():
    # Single-arena model: creating a match deletes every prior match (lobby,
    # running, AND finished), so exactly one match exists afterwards.
    eng, now = _engine()
    first = eng.create_match("skirmish", {"grid": 11}, autostart=False)
    eng.join_match(first.match_id, "u1", "A", None)  # gives the old match a roster
    second = eng.create_match("deathmatch", {"score_limit": 3}, autostart=False)
    matches = eng.list_matches()
    assert [m.match_id for m in matches] == [second.match_id]
    # The replaced match and its state are gone.
    try:
        eng.get_info(first.match_id)
        assert False, "expected the replaced match to be deleted"
    except KeyError:
        pass
    assert eng._store.get_match_state(first.match_id) is None


def test_create_match_replaces_finished_match():
    # A finished match is also cleared by the next create, so a stale result never
    # lingers alongside the new arena.
    eng, now = _engine()
    mid = _start_deathmatch(eng)        # score_limit 1: ends on the first kill
    now[0] += 600.0
    eng.scene_view(mid)                 # lazy-finalize to finished
    assert eng.get_info(mid).phase.value == "finished"
    new = eng.create_match("skirmish", {"grid": 9}, autostart=False)
    assert [m.match_id for m in eng.list_matches()] == [new.match_id]


def test_named_room_create_leaves_single_match():
    # The named-room deterministic-id path still leaves exactly one match: a prior
    # match under a different id is removed, and re-creating the same room reuses it.
    eng, _ = _engine()
    stray = eng.create_match("deathmatch", {"score_limit": 3}, autostart=False)
    a = eng.create_match("skirmish", {"grid": 11}, autostart=False, room="friday")
    assert a.match_id.startswith("room-")
    assert [m.match_id for m in eng.list_matches()] == [a.match_id]
    # Re-creating the same room reuses the same id and still leaves one match.
    b = eng.create_match("skirmish", {"grid": 11}, autostart=False, room="friday")
    assert b.match_id == a.match_id
    assert [m.match_id for m in eng.list_matches()] == [a.match_id]


def test_current_match_resolves_single_and_empty():
    eng, now = _engine()
    assert eng.current_match() is None          # no match yet
    info = eng.create_match("skirmish", {"grid": 11}, autostart=False)
    cur = eng.current_match()
    assert cur is not None and cur.match_id == info.match_id
    assert cur.phase.value == "lobby"


def test_current_match_prefers_running_over_finished():
    # If a finished and a running match ever coexist, the resolver prefers running.
    # Build the pair directly in the store, bypassing create_match's wipe.
    eng, now = _engine()
    fin = eng.create_match("deathmatch", {"score_limit": 1, "arena_size": 12}, autostart=False)
    # Mark the first match finished in place.
    meta, ver = eng._store.get_match_meta(fin.match_id)
    meta["phase"] = "finished"
    eng._store.put_match_meta(fin.match_id, meta, ver)
    eng._store.update_match_index(fin.match_id, "deathmatch", "finished")
    # A running match added straight to the store (no wipe).
    run = MatchInfo(match_id="run-1", game_id="deathmatch", phase=MatchPhase.running,
                    config={}, players=[], created_at=_now_iso())
    eng._store.put_match_meta("run-1", run.model_dump(mode="json"), None)
    eng._store.update_match_index("run-1", "deathmatch", "running")
    assert eng.current_match().match_id == "run-1"


def test_endless_match_catchup_is_bounded():
    # Regression (the stale-match brick): an endless real-time match (skirmish with
    # rounds_to_win=0 never fires a result) must not re-simulate an unbounded idle gap
    # on every read. A read after a long abandonment advances at most _MAX_CATCHUP
    # ticks, so the projection stays cheap and the hot read endpoints cannot time out.
    from arena.engine import _MAX_CATCHUP
    eng, now = _engine()
    info = eng.create_match("skirmish", {"grid": 11, "rounds_to_win": 0}, autostart=False)
    mid = info.match_id
    eng.join_match(mid, "u1", "A", None)
    eng.join_match(mid, "u2", "B", None)
    eng.start_match(mid)
    # Jump the clock two days ahead: raw elapsed would be ~1.7M ticks at 10 Hz.
    now[0] += 2 * 24 * 3600.0
    scene = eng.scene_view(mid)
    assert scene["phase"] == "running"            # endless: never finishes on its own
    assert scene["tick"] <= _MAX_CATCHUP          # catch-up bounded, not millions
    # Reads do not persist, so a second read re-projects from the same seed and agrees
    # (deterministic), and is still bounded rather than bricked.
    assert eng.scene_view(mid)["tick"] == scene["tick"]


def test_action_after_long_idle_snaps_wall_clock():
    # When the idle gap is clamped, the next action resumes the match at "now" rather
    # than crawling forward one _MAX_CATCHUP bound per action: after one action, the
    # persisted wall baseline is ~now, so no large catch-up remains.
    from arena.engine import _MAX_CATCHUP
    eng, now = _engine()
    info = eng.create_match("skirmish", {"grid": 11, "rounds_to_win": 0}, autostart=False)
    mid = info.match_id
    eng.join_match(mid, "u1", "A", None)
    eng.join_match(mid, "u2", "B", None)
    eng.start_match(mid)
    now[0] += 2 * 24 * 3600.0
    eng.submit_actions(mid, "u1", [{"type": "wait"}])   # clamped catch-up, then snap
    rec, _ = eng._store.get_match_state(mid)
    assert abs(rec.last_wall_ms - eng._now_ms()) < 1000     # baseline snapped to now
    persisted_tick = rec.last_tick
    # The next read has essentially nothing to catch up: no second cap-sized jump.
    assert eng.scene_view(mid)["tick"] - persisted_tick < _MAX_CATCHUP


def test_finite_match_still_finalizes_on_idle_read():
    # The cap must stay above the largest finite horizon so the designed behavior
    # holds: a finite match that hit its time limit while nobody watched finalizes on
    # the next read. Deathmatch times out at time_limit_ticks (default 6000 < cap).
    eng, now = _engine()
    mid = _start_deathmatch(eng, score_limit=99)  # unreachable by kills; only the timer ends it
    now[0] += 700.0                               # 7000 ticks > 6000-tick timeout
    scene = eng.scene_view(mid)
    assert scene["phase"] == "finished"           # reached its time limit on an idle read


def test_lobby_shows_board_and_players_before_start():
    import pytest
    eng, now = _engine()
    info = eng.create_match("skirmish", {"grid": 11}, autostart=False)
    mid = info.match_id
    scene = eng.scene_view(mid)                          # board exists at create, no players
    assert scene["scene"]["walls"] and scene["scene"]["players"] == []
    eng.join_match(mid, "u1", "Echo", None)             # player appears on the board in lobby
    assert [p["name"] for p in eng.scene_view(mid)["scene"]["players"]] == ["Echo"]
    with pytest.raises(ValueError):                      # agents cannot read before start
        eng.agent_view(mid, "u1")
    t0 = eng.scene_view(mid)["tick"]; now[0] += 5.0      # lobby does not tick
    assert eng.scene_view(mid)["tick"] == t0
