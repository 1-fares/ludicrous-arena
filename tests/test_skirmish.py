"""Rules tests for the grid-maze shooter. Drive the game methods directly."""

import json

from arena import registry
import arena.games  # noqa: F401
from arena.games.skirmish import _rings
from arena.models import PlayerSlot


def _slots(n):
    return [PlayerSlot(player_id=f"p{i+1}", user_id=f"u{i}", display_name=f"U{i}") for i in range(n)]


def _game():
    return registry.get("skirmish")


def test_registered_and_maze_has_no_border_and_spawns_off_ring0():
    g = _game()
    st = g.init_state({"grid": 9, "seed": 2}, _slots(4))
    walls = {(x, y) for x, y in st.walls}
    # No border ring: the outermost row/column is open floor (the collapsing void forms
    # the edge). Interior cover may still exist; nothing sits on the perimeter.
    assert not any((i, 0) in walls or (0, i) in walls or (i, 8) in walls or (8, i) in walls
                   for i in range(9))
    # No fighter spawns on a wall or on ring 0 (the outermost ring, first to fall).
    for f in st.fighters.values():
        assert (f.x, f.y) not in walls
        assert min(f.x, f.y, 8 - f.x, 8 - f.y) >= 1
    assert len(st.fighters) == 4


def test_move_blocked_by_wall():
    g = _game()
    st = g.init_state({"grid": 9, "seed": 1, "collapse": False}, _slots(2))
    f = st.fighters["p1"]
    f.x, f.y, f.facing = 2, 2, 1   # facing East
    f.move_cd = 0
    st.fighters["p2"].x, st.fighters["p2"].y = 7, 7   # keep p2 out of the lane
    st.walls = [w for w in st.walls if tuple(w) not in {(2, 2), (3, 2)}]
    st.walls.append([3, 2])        # a wall directly ahead
    assert g.validate(st, "p1", {"type": "move", "dir": "forward"}) == \
        "blocked by a wall or another character"


def test_turn_changes_facing_and_is_free():
    g = _game()
    st = g.init_state({"grid": 9, "seed": 1}, _slots(2))
    st.fighters["p1"].facing = 0  # N
    g.apply(st, "p1", {"type": "turn", "to": "right"})
    assert st.fighters["p1"].facing == 1  # E
    g.apply(st, "p1", {"type": "turn", "to": "around"})
    assert st.fighters["p1"].facing == 3  # W
    assert st.fighters["p1"].move_cd == 0  # turning costs no cooldown


def test_fire_cooldown_enforced():
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "fire_cooldown": 10}, _slots(2))
    assert g.validate(st, "p1", {"type": "fire"}) is None
    g.apply(st, "p1", {"type": "fire"})
    assert g.validate(st, "p1", {"type": "fire"}) == "gun is reloading"
    for _ in range(10):
        g.tick(st, 0.1)
    assert g.validate(st, "p1", {"type": "fire"}) is None


def test_shot_takes_a_heart_and_three_eliminate():
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "hearts": 3, "fire_range": 6}, _slots(2))
    # Clear the lane: p1 at (3,5) facing East, p2 two cells east at (5,5).
    st.walls = [w for w in st.walls if not (w[1] == 5 and 3 <= w[0] <= 6)]
    p1, p2 = st.fighters["p1"], st.fighters["p2"]
    p1.x, p1.y, p1.facing = 3, 5, 1
    p2.x, p2.y = 5, 5
    for shot in range(3):
        p1.fire_cd = 0
        g.apply(st, "p1", {"type": "fire"})
        for _ in range(8):  # let the bullet reach p2
            g.tick(st, 0.1)
        assert p2.hearts == 3 - (shot + 1), f"after shot {shot+1}"
    assert not p2.alive


def test_bullets_and_last_hit_are_observable():
    # A bullet in flight is visible to the target (so it can dodge), and a hit
    # records the bearing it came from. p1 at (3,5) facing East fires at p2 at (5,5).
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "hearts": 3, "fire_range": 6}, _slots(2))
    st.walls = [w for w in st.walls if not (w[1] == 5 and 3 <= w[0] <= 6)]
    p1, p2 = st.fighters["p1"], st.fighters["p2"]
    p1.x, p1.y, p1.facing = 3, 5, 1
    p2.x, p2.y, p2.facing = 5, 5, 1   # p2 also faces East, so the shot comes from behind
    g.apply(st, "p1", {"type": "fire"})
    g.tick(st, 0.1)
    # p2 sees the in-flight bullet even though it is travelling toward p2 from behind.
    bullets = g.observe(st, "p2")["view"]["bullets"]
    assert bullets and all({"x", "y", "dx", "dy"} <= set(b) for b in bullets)
    for _ in range(8):                # let it land
        g.tick(st, 0.1)
    hit = g.observe(st, "p2")["you"]["last_hit"]
    assert hit is not None and hit["dir"] == "E" and hit["from"] == "behind"


def test_fire_and_move_cooldowns_are_independent():
    # Shoot-and-scoot: firing is allowed while the move cooldown is active (and vice
    # versa). can_fire tracks only the gun, so it stays True during a move cooldown.
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "move_cooldown": 3}, _slots(2))
    f = st.fighters["p1"]
    f.x, f.y, f.facing = 5, 5, 1
    f.fire_cd = 0
    g.apply(st, "p1", {"type": "move", "dir": "forward"})   # starts the move cooldown
    assert f.move_cd > 0 and f.fire_cd == 0
    you = g.observe(st, "p1")["you"]
    assert you["can_move"] is False and you["can_fire"] is True
    assert g.validate(st, "p1", {"type": "fire"}) is None   # firing mid-move is allowed


def test_round_resets_and_respawns_without_scoring():
    # Surviving a round is worth nothing; only kills score. The round still resets
    # so the downed fighter respawns and the hunt continues.
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "hearts": 1, "intermission": 2,
                       "score_to_win": 10}, _slots(2))
    st.fighters["p2"].alive = False  # p1 is last standing, but did not earn the kill
    g.tick(st, 0.1)                  # round resolves -> intermission, no point
    assert st.fighters["p1"].frags == 0 and st.phase == "intermission"
    for _ in range(3):               # intermission elapses -> respawn
        g.tick(st, 0.1)
    assert st.phase == "fighting" and st.fighters["p2"].alive and st.round == 2


def test_elimination_scores_the_shooter():
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "hearts": 1, "fire_range": 6,
                       "score_to_win": 10}, _slots(2))
    # Clear the lane: p1 at (3,5) facing East, p2 two cells east at (5,5).
    st.walls = [w for w in st.walls if not (w[1] == 5 and 3 <= w[0] <= 6)]
    p1, p2 = st.fighters["p1"], st.fighters["p2"]
    p1.x, p1.y, p1.facing = 3, 5, 1
    p2.x, p2.y = 5, 5
    p1.fire_cd = 0
    g.apply(st, "p1", {"type": "fire"})
    for _ in range(8):               # let the bullet reach p2
        g.tick(st, 0.1)
    assert not p2.alive
    assert p1.frags == 1             # the kill scored, the survival did not


def test_round_limit_force_resets_stalled_round_without_scoring():
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "round_limit": 5, "intermission": 2,
                       "hearts": 3}, _slots(2))
    st.fighters["p2"].hearts = 1   # nobody dies this round; it just stalls
    for _ in range(6):             # exceed the round limit
        g.tick(st, 0.1)
    # The stalled round resets to respawn, but outlasting it scores nothing.
    assert st.fighters["p1"].frags == 0 and st.fighters["p2"].frags == 0
    assert st.phase == "intermission"


def test_match_ends_at_rounds_to_win():
    # The match ends when a fighter reaches rounds_to_win round-wins. With the limit at
    # 1, winning a single round (being the last standing) takes the match. score_to_win
    # is no longer a match-ending condition.
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "hearts": 1, "intermission": 1,
                       "rounds_to_win": 1, "collapse": False, "drop_after": 0}, _slots(2))
    assert g.result(st) is None
    st.fighters["p1"].frags = 99      # frags alone never end the match anymore
    assert g.result(st) is None
    st.fighters["p2"].alive = False   # p1 is the last in-round fighter
    g.tick(st, 0.1)                   # round resolves -> p1 wins the round
    assert st.fighters["p1"].round_wins == 1 and st.phase == "intermission"
    res = g.result(st)
    assert res is not None and res.winners == ["p1"]
    assert res.reason == "first to 1 round wins"


def test_vision_only_forward_and_blocked_by_walls():
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "fov_deg": 30, "sight": 12}, _slots(2))
    st.walls = [w for w in st.walls if not (w[1] == 5 and 3 <= w[0] <= 8)]
    p1, p2 = st.fighters["p1"], st.fighters["p2"]
    p1.x, p1.y, p1.facing = 3, 5, 1   # facing East
    p2.x, p2.y = 7, 5                  # directly ahead
    obs = g.observe(st, "p1")
    assert any(e["name"] == p2.name for e in obs["view"]["enemies"])
    # Turn away (West): the enemy behind must not be visible.
    p1.facing = 3
    assert g.observe(st, "p1")["view"]["enemies"] == []
    # Wall between them blocks line of sight even when ahead.
    p1.facing = 1
    st.walls.append([5, 5])
    assert g.observe(st, "p1")["view"]["enemies"] == []


def test_no_timeout_round_does_not_end_on_the_clock():
    # With round_limit 0, the clock must not end a round. Disable drop_after here so
    # the (action-less) test fighters are not downed for inactivity; this isolates
    # the round-limit behaviour from the drop-out behaviour.
    g = _game()
    st = g.init_state({"grid": 11, "round_limit": 0, "drop_after": 0, "collapse": False},
                      _slots(2))
    for _ in range(1200):          # far past any old timeout default
        g.tick(st, 0.1)
    assert st.phase == "fighting" and st.round == 1


def test_config_validation_rejects_dangerous_values():
    from arena.config import validate_config
    g = _game()
    for bad in ({"wall_density": 1.01}, {"wall_density": -0.1}, {"grid": 0},
                {"grid": 999}, {"hearts": 0}, {"grid": True}):
        try:
            validate_config(g.meta, bad)
            assert False, f"expected rejection of {bad}"
        except ValueError:
            pass
    validate_config(g.meta, {"grid": 13, "wall_density": 0.3})  # valid passes


def test_maze_extreme_density_terminates_without_stacking():
    # _build_maze must terminate (bounded loop, clamped density) and give every
    # fighter a distinct spawn even on a small, dense grid.
    g = _game()
    st = g.init_state({"grid": 7, "wall_density": 5.0, "seed": 3}, _slots(4))
    spots = [(f.x, f.y) for f in st.fighters.values()]
    assert len(set(spots)) == len(spots)


def test_vision_cone_expands_and_occludes():
    g = _game()
    st = g.init_state({"grid": 13, "seed": 1, "sight": 5}, _slots(1))
    # Clear all cover so the cone is unobstructed (there is no border wall to keep).
    st.walls = []
    p1 = st.fighters["p1"]
    p1.x, p1.y, p1.facing = 6, 6, 0     # facing North in open space
    cells = g.observe(st, "p1")["view"]["cells"]
    by_d = {}
    for c in cells:
        by_d.setdefault(c["forward"], set()).add(c["right"])
    assert by_d[1] == {-1, 0, 1}                         # 3 cells immediately ahead
    assert by_d[2] == {-2, -1, 0, 1, 2}                  # then 5
    assert by_d[3] == {-3, -2, -1, 0, 1, 2, 3}           # then 7

    # A wall directly ahead is visible but hides the column behind it.
    st.walls.append([6, 5])             # forward 1, right 0 (one cell north)
    view = g.observe(st, "p1")["view"]
    assert any(c["forward"] == 1 and c["right"] == 0 and c["what"] == "wall" for c in view["cells"])
    assert not any(c["forward"] >= 2 and c["right"] == 0 for c in view["cells"])


def test_idle_exposes_position_to_all_enemies():
    # Stay in one cell past expose_ticks and every enemy sees you, through walls and
    # outside their cone. Moving clears it. This is what makes camping lose.
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "expose_ticks": 5}, _slots(2))
    p1, p2 = st.fighters["p1"], st.fighters["p2"]
    p1.x, p1.y = 3, 3
    p2.x, p2.y, p2.facing = 8, 8, 0   # far away, facing elsewhere
    st.walls.append([5, 5])           # a wall between them; pinged ignores it
    for _ in range(6):                # p1 never moves -> exposed
        g.tick(st, 0.1)
    assert p1.exposed
    pinged = g.observe(st, "p2")["view"]["pinged"]
    assert any(e["name"] == p1.name and (e["x"], e["y"]) == (3, 3) for e in pinged)
    # p1 moves -> no longer exposed, drops out of the pings next tick.
    p1.facing = 1; p1.move_cd = 0
    g.apply(st, "p1", {"type": "move", "dir": "forward"})
    g.tick(st, 0.1)
    assert not st.fighters["p1"].exposed
    assert g.observe(st, "p2")["view"]["pinged"] == []


def test_new_ground_scores_territory_up_to_the_cap():
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "cell_bonus": 0.5, "territory_cap": 1.0},
                      _slots(2))
    p1 = st.fighters["p1"]
    st.walls = [w for w in st.walls if not (w[1] == 5 and 1 <= w[0] <= 9)]  # clear a lane
    p1.x, p1.y, p1.facing = 2, 5, 1   # facing East along the open row
    for _ in range(4):                # walk forward over fresh cells
        p1.move_cd = 0
        g.apply(st, "p1", {"type": "move", "dir": "forward"})
    # Two new cells reach the cap (0.5 + 0.5); further new ground earns nothing, and
    # the visited list stops growing so state stays bounded.
    assert p1.terr == 1.0
    assert len(p1.visited) == 2
    obs = g.observe(st, "p1")["you"]
    assert obs["territory"] == 1.0 and obs["frags"] == 0 and obs["score"] == 1.0


def test_state_json_roundtrip():
    g = _game()
    st = g.init_state({"grid": 11, "seed": 4}, _slots(3))
    g.apply(st, "p1", {"type": "fire"})
    for _ in range(3):
        g.tick(st, 0.1)
    g.apply(st, "p2", {"type": "move", "dir": "forward"})   # exercise territory/idle fields
    for _ in range(3):
        g.tick(st, 0.1)
    restored = g.decode_state(json.loads(json.dumps(g.encode_state(st))))
    assert g.render(restored) == g.render(st)


def test_drop_after_removes_an_inactive_fighter():
    # A client that submits no action for drop_after ticks is removed from the game
    # entirely (no phantom body). An active player is untouched.
    g = _game()
    st = g.init_state({"grid": 11, "drop_after": 3, "score_to_win": 10}, _slots(2))
    for _ in range(5):
        g.apply(st, "p1", {"type": "wait"})   # p1 stays active; p2 never acts
        g.tick(st, 0.1)
    assert "p2" not in st.fighters and "p1" in st.fighters
    assert g.active_players(st) == {"p1"}


def test_dead_player_waiting_to_respawn_is_not_dropped():
    # The bug behind "5-player game restarted with 3": a fighter eliminated in a long
    # round stops acting (nothing to do while dead) and must NOT be dropped for it.
    g = _game()
    st = g.init_state({"grid": 13, "drop_after": 3, "score_to_win": 99}, _slots(3))
    st.fighters["p3"].alive = False          # eliminated; p1 and p2 still fighting
    for _ in range(10):                      # long round, well past drop_after
        g.apply(st, "p1", {"type": "wait"})
        g.apply(st, "p2", {"type": "wait"})
        g.tick(st, 0.1)
    assert "p3" in st.fighters                # dead-and-waiting is kept
    assert st.phase == "fighting" and "p1" in st.fighters and "p2" in st.fighters


# ---- collapsing arena -----------------------------------------------------

def _collapse_cfg(**over):
    # A fast, fully determined collapse schedule for tests. On grid 11 rings_total is 5
    # and ring indices run 0..5 (edge to centre); keep_rings 2 gives keep_from 3, so
    # rings 0,1,2 fall and rings 3,4,5 are the kept core.
    cfg = {"grid": 11, "seed": 1, "collapse": True, "collapse_start": 5,
           "ring_interval": 100, "decay_ticks": 5, "decay_stages": 4, "keep_rings": 2,
           "drop_after": 0, "hearts": 3, "score_to_win": 99}
    cfg.update(over)
    return cfg


def _clear_interior(st):
    st.walls = [w for w in st.walls if w[0] in (0, st.grid - 1) or w[1] in (0, st.grid - 1)]


def test_ring_index_is_distance_from_grid_edge():
    # Ring 0 is the outermost row/column of the grid (no border wall); the index grows
    # toward the centre, which is the highest ring. rings_total stays (grid-1)//2.
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "collapse": True}, _slots(1))
    st.walls = []                              # introspect pure geometry
    rings, rings_total, center = _rings(st)
    assert rings_total == 5 and center == (5, 5)
    assert rings[(0, 0)] == 0 and rings[(0, 5)] == 0 and rings[(10, 7)] == 0
    assert rings[(1, 1)] == 1
    assert rings[(5, 5)] == 5                  # centre is the highest ring


def test_outer_edge_is_floor_and_collapses_on_schedule():
    # The outermost grid cells are open floor, not wall, and are ring 0: the first ring
    # to fall. A fighter may step onto an edge cell while it is still solid.
    g = _game()
    st = g.init_state(_collapse_cfg(), _slots(1))
    _clear_interior(st)
    walls = {(x, y) for x, y in st.walls}
    assert (0, 0) not in walls and (0, 5) not in walls and (10, 0) not in walls
    assert (0, 5) not in g._void_at(st, 9)     # ring 0: begin=5, fall=10
    assert (0, 5) in g._void_at(st, 10)
    p1 = st.fighters["p1"]
    p1.x, p1.y, p1.facing, p1.move_cd = 1, 5, 3, 0   # facing West onto solid edge (0,5)
    st.round_start = st.tick                          # elapsed 0: edge still solid
    assert g.validate(st, "p1", {"type": "move", "dir": "forward"}) is None


def test_walls_fall_on_ring_schedule():
    # A wall on an outer ring cracks then falls on the same schedule as the floor: while
    # standing it blocks vision; once void it is a hole (no block, no occlusion) and
    # moves from walls/walls_cracking into floor.void. Inner-core walls never fall.
    g = _game()
    st = g.init_state(_collapse_cfg(collapse_start=0, ring_interval=10, decay_ticks=5,
                                    decay_stages=4, keep_rings=2, sight=12), _slots(1))
    _clear_interior(st)
    st.walls = [[1, 5], [5, 5]]                # ring 1 (collapses), ring 5 (kept core)
    p1 = st.fighters["p1"]
    p1.x, p1.y, p1.facing = 3, 5, 3            # at (3,5) facing West toward the wall
    # ring 1: begin = 0 + 1*10 = 10, fall = 15. At e=12 it is cracking, still a wall.
    st.tick = 12
    assert (1, 5) not in g._void_at(st, 12)
    cells = g.observe(st, "p1")["view"]["cells"]
    assert any(c["x"] == 1 and c["y"] == 5 and c["what"] == "wall" for c in cells)
    assert not any(c["x"] == 0 and c["y"] == 5 for c in cells)   # (0,5) hidden behind it
    r = g.render(st)
    assert [1, 5] in r["walls"]
    assert any(w["x"] == 1 and w["y"] == 5 for w in r["walls_cracking"])
    # After it falls (e >= 15) the wall is void: a hole, not a wall, not occluding.
    st.tick = 20
    assert (1, 5) in g._void_at(st, 20)
    cells = g.observe(st, "p1")["view"]["cells"]
    assert any(c["x"] == 1 and c["y"] == 5 and c["what"] == "void" for c in cells)
    assert any(c["x"] == 0 and c["y"] == 5 for c in cells)       # now visible through the hole
    # A move into the fallen wall cell is rejected (it is void). Stand on (2,5) (still
    # solid floor) facing West, so the cell ahead is the fallen wall (1,5).
    p1.x, p1.y, p1.facing, p1.move_cd = 2, 5, 3, 0
    assert g.validate(st, "p1", {"type": "move", "dir": "forward"}) == \
        "blocked by a wall or another character"
    r = g.render(st)
    assert [1, 5] not in r["walls"]                              # dropped from standing walls
    assert [1, 5] in r["floor"]["void"]                         # every hole goes here
    assert not any(w["x"] == 1 and w["y"] == 5 for w in r["walls_cracking"])
    # Inner-core wall never falls and stays a standing wall.
    assert (5, 5) not in g._void_at(st, 10_000)
    assert [5, 5] in g.render(st)["walls"]


def test_outer_ring_falls_on_schedule_and_puts_fighter_out_for_the_round():
    # An outer-ring tile falls at its scheduled tick. A fighter standing alive on it
    # goes out for the round (0 points until it respawns next round); a fighter in the
    # kept core survives.
    g = _game()
    st = g.init_state(_collapse_cfg(), _slots(2))
    _clear_interior(st)
    p1, p2 = st.fighters["p1"], st.fighters["p2"]
    p1.x, p1.y, p1.px, p1.py = 0, 0, 0, 0     # ring 0 (outer edge): begin=5, fall=10
    p2.x, p2.y, p2.px, p2.py = 5, 5, 5, 5     # centre: kept core, never falls
    # The schedule is a pure function of elapsed ticks: solid before the fall, void at it.
    assert (0, 0) not in g._void_at(st, 9)
    assert (0, 0) in g._void_at(st, 10)
    assert (5, 5) not in g._void_at(st, 10_000)
    for _ in range(10):
        g.tick(st, 0.1)
    assert st.tick == 10
    assert p1.out and not p1.alive and p1.fell_tick == 10
    assert not p2.out and p2.alive
    # An out fighter scores 0 for the round and reports it, even with frags banked.
    p1.frags = 7
    you = g.observe(st, "p1")["you"]
    assert you["out"] is True and you["score"] == 0
    assert "fell into the void" in you["out_reason"]
    assert g.observe(st, "p1")["scores"][p1.name] == 0


def test_downed_fighter_on_collapsing_tile_goes_out():
    # A downed (not alive) fighter on a tile that falls also drops into the void and
    # goes out. A body no longer hovers over a hole, and it does not respawn.
    g = _game()
    st = g.init_state(_collapse_cfg(), _slots(2))
    _clear_interior(st)
    p1 = st.fighters["p1"]
    p1.x, p1.y, p1.px, p1.py = 0, 0, 0, 0     # ring 0 (outer edge): falls at tick 10
    p1.alive = False                          # downed before the floor falls
    for _ in range(10):
        g.tick(st, 0.1)
    assert p1.out and not p1.alive and p1.fell_tick == 10


def test_collapse_continues_to_centre_and_forces_resolution():
    # Regression for a frozen match: with a large permanent core, two fighters that never
    # engage sit on the solid centre forever and the round never ends. With the default
    # keep_rings 0 the floor decays all the way down to the single centre cell, so the
    # round must resolve on its own (here both fall, a draw) and the match keeps cycling
    # instead of freezing. Neither fighter ever acts; only the collapse can end the round.
    g = _game()
    st = g.init_state(_collapse_cfg(collapse_start=0, ring_interval=1, decay_ticks=1,
                                    keep_rings=0, intermission=1, rounds_to_win=99),
                      _slots(2))
    _clear_interior(st)
    st.fighters["p1"].x, st.fighters["p1"].y = 1, 1
    st.fighters["p2"].x, st.fighters["p2"].y = 9, 9
    start = st.round
    for _ in range(100):
        g.tick(st, 0.1)
    assert st.round > start                  # rounds kept resolving; the match never froze


def test_vision_sees_across_void():
    # A void tile is a hole, not a wall: it does not block line of sight. Looking from
    # the kept core out across the fallen edge, the farthest fallen tile is still
    # visible (a wall there would have occluded it), and fallen tiles read as "void".
    g = _game()
    st = g.init_state(_collapse_cfg(collapse_start=0, ring_interval=1, decay_ticks=1,
                                    decay_stages=2, sight=12), _slots(1))
    _clear_interior(st)
    p1 = st.fighters["p1"]
    p1.x, p1.y, p1.facing = 5, 5, 0           # centre, kept core, facing North
    st.tick = 20                              # rings 0,1,2 along the column have fallen
    void = g._void_at(st, st.tick - st.round_start)
    assert {(5, 0), (5, 1), (5, 2)} <= void and (5, 3) not in void
    cells = g.observe(st, "p1")["view"]["cells"]
    voids = {(c["x"], c["y"]) for c in cells if c["what"] == "void"}
    # All three collinear void cells are visible, the farthest (5,0) included: a void
    # cell does not occlude the ones behind it (a wall there would have hidden them).
    assert {(5, 0), (5, 1), (5, 2)} <= voids


def test_move_into_void_is_rejected():
    g = _game()
    st = g.init_state(_collapse_cfg(collapse_start=0, ring_interval=1, decay_ticks=1,
                                    decay_stages=2), _slots(1))
    _clear_interior(st)
    p1 = st.fighters["p1"]
    p1.x, p1.y, p1.facing, p1.move_cd = 5, 3, 0, 0   # ring 3 (kept), facing North toward (5,2)
    st.tick = 20
    assert (5, 2) in g._void_at(st, st.tick - st.round_start)
    assert g.validate(st, "p1", {"type": "move", "dir": "forward"}) == \
        "blocked by a wall or another character"
    p1.facing = 2                             # South into the kept core (5,4): allowed
    assert g.validate(st, "p1", {"type": "move", "dir": "forward"}) is None


def test_coarse_enemy_hp_is_reported():
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "hearts": 3, "collapse": False, "sight": 12},
                      _slots(2))
    st.walls = [w for w in st.walls if not (w[1] == 5 and 3 <= w[0] <= 8)]
    p1, p2 = st.fighters["p1"], st.fighters["p2"]
    p1.x, p1.y, p1.facing = 3, 5, 1           # facing East along the open lane
    p2.x, p2.y = 6, 5

    def enemy_hp():
        obs = g.observe(st, "p1")["view"]
        e = next(e for e in obs["enemies"] if e["name"] == p2.name)
        c = next(c for c in obs["cells"] if c.get("name") == p2.name and c["what"] == "enemy")
        assert e["hp"] == c["hp"]
        return e["hp"]

    assert enemy_hp() == "full"               # hearts == hearts_max
    p2.hearts = 2
    assert enemy_hp() == "wounded"
    p2.hearts = 1
    assert enemy_hp() == "critical"           # one heart left


def test_fallen_fighter_is_out_for_the_round_then_respawns():
    # Falling is round-scoped: a fighter that fell is out for the current round, then
    # respawns (out cleared, fell_tick cleared, alive restored) when the next round
    # begins. round_wins for the round goes to the last fighter standing.
    g = _game()
    st = g.init_state(_collapse_cfg(intermission=1, rounds_to_win=10), _slots(2))
    _clear_interior(st)
    p1, p2 = st.fighters["p1"], st.fighters["p2"]
    p1.x, p1.y, p1.px, p1.py = 0, 0, 0, 0     # ring 0: falls at tick 10
    p2.x, p2.y, p2.px, p2.py = 5, 5, 5, 5     # kept core: survives
    for _ in range(10):
        g.tick(st, 0.1)
    assert p1.out and not p1.alive             # fell this round
    assert p2.round_wins == 1 and st.phase == "intermission"   # last standing won the round
    for _ in range(2):                         # intermission elapses -> new round
        g.tick(st, 0.1)
    assert st.phase == "fighting" and st.round == 2
    assert not p1.out and p1.fell_tick is None and p1.alive     # respawned, out cleared
    assert p2.round_wins == 1                   # round_wins persists across rounds


def test_last_in_round_fighter_wins_the_round():
    # A round ends when at most one fighter is in-round; that survivor's round_wins
    # increments and a fresh round starts.
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "hearts": 1, "intermission": 1,
                       "collapse": False, "drop_after": 0, "rounds_to_win": 10}, _slots(3))
    st.fighters["p2"].alive = False
    st.fighters["p3"].alive = False            # only p1 remains in-round
    g.tick(st, 0.1)
    assert st.fighters["p1"].round_wins == 1 and st.phase == "intermission"
    for _ in range(2):
        g.tick(st, 0.1)
    assert st.phase == "fighting" and st.round == 2   # a fresh round started


def test_out_is_cleared_every_round_not_permanent():
    # out is round-scoped, not a permanent match elimination: the same fighter falls,
    # respawns, and falls again round after round.
    g = _game()
    st = g.init_state(_collapse_cfg(intermission=1, rounds_to_win=10), _slots(2))
    _clear_interior(st)
    p1, p2 = st.fighters["p1"], st.fighters["p2"]
    p2.x, p2.y, p2.px, p2.py = 5, 5, 5, 5      # kept core: survives every round
    for expected_round in (2, 3):
        p1.x, p1.y, p1.px, p1.py = 0, 0, 0, 0  # ring 0
        st.round_start = st.tick               # collapse timing from here: ring 0 falls 10 ticks on
        for _ in range(10):
            g.tick(st, 0.1)
        assert p1.out and st.phase == "intermission"
        for _ in range(2):                     # intermission -> respawn
            g.tick(st, 0.1)
        assert not p1.out and p1.alive and st.round == expected_round


def test_drop_after_default_off_keeps_idle_present_player():
    # The default drop_after is 0 (off): an idle-but-present fighter is never evicted,
    # even across a long round, so a brief bot pause cannot make a player vanish.
    g = _game()
    st = g.init_state({"grid": 13, "collapse": False, "rounds_to_win": 99}, _slots(3))
    assert st.cfg["drop_after"] == 0
    for _ in range(400):                       # long round, nobody acts
        g.tick(st, 0.1)
    assert set(st.fighters.keys()) == {"p1", "p2", "p3"}
    assert g.active_players(st) == {"p1", "p2", "p3"}


def test_encode_decode_roundtrips_round_wins():
    g = _game()
    st = g.init_state(_collapse_cfg(), _slots(2))
    f = st.fighters["p1"]
    f.round_wins, f.out, f.alive, f.fell_tick = 4, True, False, 12
    restored = g.decode_state(json.loads(json.dumps(g.encode_state(st))))
    rf = restored.fighters["p1"]
    assert rf.round_wins == 4 and rf.out is True and rf.fell_tick == 12
    assert restored.fighters["p2"].round_wins == 0    # default survives for an untouched fighter
    assert g.render(restored) == g.render(st)


def test_collapse_off_keeps_the_floor_solid():
    g = _game()
    st = g.init_state({"grid": 11, "collapse": False, "drop_after": 0}, _slots(2))
    for _ in range(400):                      # well past any collapse schedule
        g.tick(st, 0.1)
    assert g._void_at(st, st.tick) == set()
    assert all(not f.out for f in st.fighters.values())
    render = g.render(st)
    assert render["floor"]["void"] == [] and render["collapse"]["on"] is False


def test_encode_decode_roundtrips_out_fields():
    g = _game()
    st = g.init_state(_collapse_cfg(), _slots(2))
    f = st.fighters["p1"]
    f.out, f.alive, f.fell_tick = True, False, 17
    restored = g.decode_state(json.loads(json.dumps(g.encode_state(st))))
    rf = restored.fighters["p1"]
    assert rf.out is True and rf.fell_tick == 17 and rf.alive is False
    # Defaults survive for an untouched fighter.
    assert restored.fighters["p2"].out is False and restored.fighters["p2"].fell_tick is None
    assert g.render(restored) == g.render(st)


def test_decode_state_fills_new_config_defaults():
    """A match created before a config key existed must still decode and run.

    Regression: a running match whose stored config predates rounds_to_win and the
    collapse knobs used to 404 with KeyError when the rules read the missing key.
    decode_state now merges the config defaults, so older configs are forward-safe.
    """
    g = _game()
    data = g.encode_state(g.init_state({"grid": 13}, _slots(2)))
    for k in ("rounds_to_win", "collapse", "ring_interval", "decay_ticks",
              "decay_stages", "keep_rings", "drop_after"):
        data["cfg"].pop(k, None)
    st = g.decode_state(data)
    assert st.cfg["rounds_to_win"] == 10
    assert st.cfg["collapse"] is True
    # The rules paths that read the new keys must not raise.
    g.result(st)
    g.observe(st, "p1")
    g.render(st)


def test_you_ground_reports_standing_tile_decay():
    """An agent must see the state of the tile under it, not only forward cells.

    The vision cone starts ahead, so without you.ground an agent gets no signal
    that the ground beneath it is cracking until it is too late to step off.
    """
    g = _game()
    # Fast collapse so an outer tile cracks quickly; small grace.
    cfg = {"grid": 13, "collapse": True, "collapse_start": 0,
           "ring_interval": 1, "decay_ticks": 20, "keep_rings": 1}
    st = g.init_state(cfg, _slots(1))
    f = next(iter(st.fighters.values()))
    # Park the fighter on an outer ring-0 cell (an edge cell) and advance time.
    f.x, f.y = 1, 6
    st.tick = st.round_start + 5      # ring 0 began cracking at e=0
    obs = g.observe(st, f_id := next(iter(st.fighters)))
    ground = obs["you"]["ground"]
    assert ground["state"] in ("cracking", "void")
    assert ground["falls_in"] is not None and ground["falls_in"] <= 20
    # A central kept-core tile never falls: falls_in is null.
    f.x, f.y = 6, 6
    assert g.observe(st, f_id)["you"]["ground"]["falls_in"] is None
