"""Rules tests for the grid-maze shooter. Drive the game methods directly."""

import json

from arena import registry
import arena.games  # noqa: F401
from arena.models import PlayerSlot


def _slots(n):
    return [PlayerSlot(player_id=f"p{i+1}", user_id=f"u{i}", display_name=f"U{i}") for i in range(n)]


def _game():
    return registry.get("skirmish")


def test_registered_and_maze_bordered():
    g = _game()
    st = g.init_state({"grid": 9, "seed": 2}, _slots(4))
    walls = {(x, y) for x, y in st.walls}
    # The whole border is wall; no fighter spawns on a wall.
    assert all((i, 0) in walls and (0, i) in walls for i in range(9))
    assert all((f.x, f.y) not in walls for f in st.fighters.values())
    assert len(st.fighters) == 4


def test_move_blocked_by_wall():
    g = _game()
    st = g.init_state({"grid": 9, "seed": 1}, _slots(2))
    f = st.fighters["p1"]
    f.x, f.y, f.facing = 1, 1, 3   # facing West, against the border wall at x=0
    f.move_cd = 0
    assert g.validate(st, "p1", {"type": "move", "dir": "forward"}) == "blocked by a wall or another character"


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


def test_game_ends_at_score_limit():
    g = _game()
    st = g.init_state({"grid": 11, "seed": 1, "hearts": 1, "intermission": 1,
                       "score_to_win": 2}, _slots(2))
    assert g.result(st) is None
    st.fighters["p1"].frags = 2
    st.fighters["p2"].alive = False
    g.tick(st, 0.1)              # resolve -> intermission, score already 2
    g.tick(st, 0.1)             # intermission ends; no respawn because limit hit
    res = g.result(st)
    assert res is not None and res.winners == ["p1"]


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
    st = g.init_state({"grid": 11, "round_limit": 0, "drop_after": 0}, _slots(2))
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
    # Clear the interior so the cone is unobstructed, keep the border.
    st.walls = [w for w in st.walls if w[0] in (0, 12) or w[1] in (0, 12)]
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
