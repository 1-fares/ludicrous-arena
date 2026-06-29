"""Deterministic rules tests that drive the games directly, bypassing the engine.
They exercise the boundary methods, state serialization, and win conditions."""

import json
import math

from arena import registry
import arena.games  # noqa: F401  -- registers games
from arena.models import PlayerSlot


def _slots(n):
    return [PlayerSlot(player_id=f"p{i+1}", user_id=f"u{i}", display_name=f"U{i}") for i in range(n)]


def test_games_registered():
    ids = {g.meta.id for g in registry.all_games()}
    assert {"skirmish", "deathmatch", "lockdown", "trading_desk"} <= ids


def test_deathmatch_kill_and_score():
    g = registry.get("deathmatch")
    st = g.init_state({"arena_size": 10, "respawn_delay": 3}, _slots(2))
    # Place p2 right next to p1 and have p1 fire straight at it.
    st.entities["p1"].x, st.entities["p1"].y = 5.0, 5.0
    st.entities["p2"].x, st.entities["p2"].y = 6.0, 5.0
    assert g.validate(st, "p1", {"type": "fire", "angle": 0.0}) is None
    g.apply(st, "p1", {"type": "fire", "angle": 0.0})  # fire toward +x
    # Cooldown now active -> second fire rejected.
    assert g.validate(st, "p1", {"type": "fire", "angle": 0.0}) == "weapon on cooldown"
    # One projectile does 34 damage; tick until it crosses the 1-unit gap.
    for _ in range(5):
        g.tick(st, 0.1)
    assert st.entities["p2"].hp < 100
    # Soften p2 to one shot from death, fire again, and confirm the kill scores.
    st.entities["p2"].hp = 20
    st.entities["p1"].cooldown = 0
    st.entities["p2"].x = 6.0  # back to where a tick-step projectile actually lands
    g.apply(st, "p1", {"type": "fire", "angle": 0.0})
    for _ in range(2):  # hit lands on tick 1; stop before respawn_delay=3 elapses
        g.tick(st, 0.1)
    assert st.entities["p1"].score >= 1
    assert st.entities["p2"].dead_for > 0


def test_deathmatch_score_limit_ends_match():
    g = registry.get("deathmatch")
    st = g.init_state({"score_limit": 2}, _slots(2))
    assert g.result(st) is None
    st.entities["p1"].score = 2
    res = g.result(st)
    assert res is not None and res.winners == ["p1"]


def test_state_roundtrips_through_json():
    # The engine persists state as JSON between every action, so encode/decode
    # must survive a real json dump/load for every game.
    for gid, cfg in [("deathmatch", {"arena_size": 10}), ("lockdown", {"fragments": 3}),
                     ("trading_desk", {"horizon": 10})]:
        g = registry.get(gid)
        st = g.init_state(cfg, _slots(2))
        for _ in range(5):
            g.tick(st, 0.1)
        encoded = json.loads(json.dumps(g.encode_state(st)))  # must be JSON-safe
        restored = g.decode_state(encoded)
        # A decoded state renders identically to the original.
        assert g.render(restored) == g.render(st)


def test_skirmish_state_roundtrips_with_collapse_fields():
    # The new per-fighter collapse fields (out, fell_tick) must survive a real JSON
    # dump/load, like every other persisted field.
    g = registry.get("skirmish")
    st = g.init_state({"grid": 11, "seed": 4, "collapse": True}, _slots(2))
    f = st.fighters["p1"]
    f.out, f.alive, f.fell_tick = True, False, 9
    encoded = json.loads(json.dumps(g.encode_state(st)))   # must be JSON-safe
    restored = g.decode_state(encoded)
    assert restored.fighters["p1"].out is True
    assert restored.fighters["p1"].fell_tick == 9
    assert restored.fighters["p2"].out is False and restored.fighters["p2"].fell_tick is None
    assert g.render(restored) == g.render(st)


def test_deathmatch_move_normalised():
    g = registry.get("deathmatch")
    st = g.init_state({"arena_size": 100, "move_speed": 10}, _slots(1) + _slots(1))
    e = st.entities["p1"]
    e.x = e.y = 50.0
    g.apply(st, "p1", {"type": "move", "dx": 3, "dy": 4})  # 3-4-5 triangle
    assert math.isclose(math.hypot(e.vx, e.vy), 10.0, rel_tol=1e-6)


def test_lockdown_fog_of_war():
    g = registry.get("lockdown")
    st = g.init_state({"grid": 8, "fragments": 2, "sight": 1, "seed": 5}, _slots(1))
    obs = g.observe(st, "p1")
    # Player starts at (0,0); only fragments within radius 1 are visible.
    for f in obs["visible"]["fragments"]:
        assert abs(f["x"]) <= 1 and abs(f["y"]) <= 1


def test_lockdown_coop_win():
    g = registry.get("lockdown")
    st = g.init_state({"grid": 4, "fragments": 1, "seed": 2}, _slots(1))
    # Walk the pawn to each fragment, then to the exit, by brute force.
    target = next(iter(st.fragments))
    _walk_to(g, st, "p1", target)
    assert st.pawns["p1"].carrying == 1
    _walk_to(g, st, "p1", st.exit)
    res = g.result(st)
    assert res is not None and res.reason.startswith("all fragments")


def test_trading_desk_deterministic_prices():
    g = registry.get("trading_desk")
    a = g.init_state({"seed": 7, "horizon": 20}, _slots(1))
    b = g.init_state({"seed": 7, "horizon": 20}, _slots(1))
    c = g.init_state({"seed": 8, "horizon": 20}, _slots(1))
    assert a.prices == b.prices            # same seed -> identical market
    assert c.prices != a.prices            # different seed -> different market
    assert len(a.prices) == 21             # horizon + 1


def test_trading_desk_trade_pnl_and_finish():
    g = registry.get("trading_desk")
    flat = {"seed": 1, "horizon": 3, "start_cash": 1000, "start_price": 100,
            "drift": 0, "volatility": 0}  # flat market: equity must equal start cash
    st = g.init_state(flat, _slots(1))
    assert g.validate(st, "p1", {"type": "trade", "delta": 5}) is None
    g.apply(st, "p1", {"type": "trade", "delta": 5}); g.tick(st, 1.0)
    assert st.traders["p1"].position == 5 and st.traders["p1"].cash == 500.0
    g.apply(st, "p1", {"type": "hold"}); g.tick(st, 1.0)
    g.apply(st, "p1", {"type": "hold"}); g.tick(st, 1.0)
    res = g.result(st)
    assert res is not None and res.reason == "horizon reached"
    assert res.scores["p1"] == 1000.0      # no price movement -> no P&L


def _props(schema):
    return schema.get("properties", {})


def test_skirmish_observation_self_name_and_rules():
    # The agent must be able to find itself in the name-keyed scoreboards and read the
    # combat constants its own schema references (fire_range), without a second call.
    g = registry.get("skirmish")
    st = g.init_state({"grid": 11, "seed": 3}, _slots(2))
    obs = g.observe(st, "p1")
    assert obs["you"]["name"] == "U0"                  # own display name is exposed
    assert obs["you"]["name"] in obs["scores"]         # so it indexes the scoreboard
    assert obs["rules"]["fire_range"] == st.cfg["fire_range"]
    assert obs["rules"]["sight"] == st.cfg["sight"]
    # Anything observe() emits must be declared in the published schema.
    you_props = _props(_props(g.meta.observation_schema)["you"])
    assert "name" in you_props
    assert "rules" in _props(g.meta.observation_schema)


def test_deathmatch_observation_you_and_projectile_velocity():
    g = registry.get("deathmatch")
    st = g.init_state({"arena_size": 20}, _slots(2))
    st.entities["p1"].x = st.entities["p1"].y = 5.0
    g.apply(st, "p1", {"type": "fire", "angle": 0.0})   # spawn a projectile
    obs = g.observe(st, "p1")
    # A `you` block with the cooldown/respawn timers a spectator never sees.
    assert obs["you"]["player_id"] == "p1"
    assert obs["you"]["cooldown"] > 0 and obs["you"]["can_fire"] is False
    assert "respawn_in" in obs["you"]
    # Projectiles carry velocity + owner so a shot is dodgeable and attributable.
    pr = obs["projectiles"][0]
    assert {"x", "y", "vx", "vy", "owner"} <= pr.keys() and pr["owner"] == "p1"
    # Win thresholds and combat geometry are self-described.
    assert obs["rules"]["score_limit"] == st.cfg["score_limit"]
    assert obs["rules"]["hit_damage"] == 34 and obs["rules"]["max_hp"] == 100
    # The spectator render stays lean (no you/rules, projectiles are just x,y).
    scene = g.render(st)
    assert "you" not in scene and set(scene["projectiles"][0]) == {"x", "y"}
    # Schema declares the new blocks.
    props = _props(g.meta.observation_schema)
    assert {"you", "rules"} <= props.keys()
    assert "vx" in _props(props["projectiles"]["items"])


def test_lockdown_observation_excludes_self_and_adds_team_aggregates():
    g = registry.get("lockdown")
    st = g.init_state({"grid": 6, "fragments": 3, "sight": 2, "seed": 1}, _slots(2))
    # Put both pawns on the same cell: the old bug listed yourself in nearby_players.
    st.pawns["p2"].x, st.pawns["p2"].y = st.pawns["p1"].x, st.pawns["p1"].y
    obs = g.observe(st, "p1")
    ids = {n["id"] for n in obs["visible"]["nearby_players"]}
    assert "p1" not in ids and "p2" in ids                 # self excluded, teammate kept
    assert "carrying" in obs["visible"]["nearby_players"][0]
    assert obs["self"]["player_id"] == "p1"
    assert obs["team"]["uncollected"] == len(st.fragments)
    # delivered + carried + uncollected == total (the published invariant).
    t = obs["team"]
    assert t["delivered"] + t["carried"] + t["uncollected"] == t["total"]
    assert "time_left" in obs


def test_lockdown_exit_cell_revealed_when_visible():
    g = registry.get("lockdown")
    st = g.init_state({"grid": 5, "fragments": 1, "sight": 1, "seed": 1}, _slots(1))
    p = st.pawns["p1"]
    p.x, p.y = st.exit                                     # stand next to/on the exit
    obs = g.observe(st, "p1")
    assert obs["visible"]["exit_visible"] is True
    assert obs["visible"]["exit"] == {"x": st.exit[0], "y": st.exit[1]}


def test_trading_desk_steps_remaining_and_config_bounds():
    from arena.config import validate_config
    g = registry.get("trading_desk")
    st = g.init_state({"seed": 1, "horizon": 5}, _slots(1))
    obs = g.observe(st, "p1")
    assert obs["steps_remaining"] == 5                     # horizon - step at step 0
    g.apply(st, "p1", {"type": "hold"}); g.tick(st, 1.0)
    assert g.observe(st, "p1")["steps_remaining"] == 4
    # Out-of-range horizon (the state-size DoS) is now rejected at config validation.
    import pytest
    with pytest.raises(ValueError):
        validate_config(g.meta, {"horizon": 10_000_000})


def test_trading_desk_validation():
    g = registry.get("trading_desk")
    st = g.init_state({"horizon": 5, "start_cash": 100, "start_price": 100,
                       "max_position": 10}, _slots(1))
    assert g.validate(st, "p1", {"type": "trade", "delta": 2})      # 200 > 100 cash
    assert g.validate(st, "p1", {"type": "trade", "delta": True})   # bool is not an int
    assert g.validate(st, "p1", {"type": "trade", "delta": 1000})   # exceeds position cap


def _walk_to(g, st, pid, cell):
    for _ in range(64):
        p = st.pawns[pid]
        if (p.x, p.y) == cell:
            return
        d = "E" if p.x < cell[0] else "W" if p.x > cell[0] else "S" if p.y < cell[1] else "N"
        g.apply(st, pid, {"type": "move", "dir": d})
        g.tick(st, 0.2)
    raise AssertionError("did not reach cell")
