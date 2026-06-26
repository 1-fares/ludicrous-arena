"""Engine-level tests: the store-backed, lazily-evaluated simulation. These drive
the real Engine against a MemoryStore with a controllable clock, so wall-clock
catch-up, optimistic writes, and lazy finalization are all deterministic.
"""

import arena.games  # noqa: F401  -- registers games
from arena.engine import Engine
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

    # The terminal record was persisted (lazy finalize), so metadata agrees and a
    # further action is rejected.
    assert eng.get_info(mid).phase.value == "finished"
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
    assert any("finite" in r for r in view["you"]["rejected"])


def test_rejected_feedback_not_clobbered_across_players():
    eng, now = _engine()
    mid = _start_deathmatch(eng, score_limit=5)
    now[0] += 0.1
    eng.submit_actions(mid, "u1", [{"type": "bogus"}])   # rejected for p1
    now[0] += 0.1
    eng.submit_actions(mid, "u2", [{"type": "bogus"}])   # must not wipe p1's feedback
    assert eng.agent_view(mid, "u1")["you"]["rejected"]
    assert eng.agent_view(mid, "u2")["you"]["rejected"]


def test_config_has_no_internal_autostart_key():
    eng, _ = _engine()
    info = eng.create_match("deathmatch", {"score_limit": 3}, autostart=True)
    assert "_autostart" not in info.config
    assert info.autostart is True


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
