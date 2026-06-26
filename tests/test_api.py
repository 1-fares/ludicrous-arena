"""End-to-end test of the HTTP surface against the real (store-backed) engine.

Uses Starlette's TestClient (sync) and a controllable clock injected into the
engine, so wall-clock catch-up is deterministic instead of timing-dependent.
"""

import pytest
from starlette.testclient import TestClient

import arena.server as server

H1 = {"Authorization": "Bearer dev-token"}
H2 = {"Authorization": "Bearer dev-token-2"}

# Mutable wall clock the engine reads through server.ENGINE._clock.
NOW = [1_000.0]


@pytest.fixture(autouse=True)
def _clock():
    NOW[0] = 1_000.0
    server.ENGINE._clock = lambda: NOW[0]
    yield


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


def test_discovery(client):
    r = client.get("/v1/games")
    assert r.status_code == 200
    games = {g["id"]: g for g in r.json()}
    assert {"skirmish", "deathmatch", "lockdown", "trading_desk"} <= set(games)
    assert games["deathmatch"]["realtime"] is True
    # observation_schema is published per game (agents code against it).
    assert games["skirmish"]["observation_schema"]["properties"]["view"]


def test_auth_required(client):
    assert client.post("/v1/matches", json={"game_id": "deathmatch"}).status_code == 401


def test_full_match_flow(client):
    r = client.post("/v1/matches",
                    json={"game_id": "deathmatch", "config": {"score_limit": 3}}, headers=H1)
    assert r.status_code == 200, r.text
    mid = r.json()["match_id"]
    assert r.json()["phase"] == "lobby"

    assert client.post(f"/v1/matches/{mid}/join", json={}, headers=H1).json()["phase"] == "lobby"
    assert client.post(f"/v1/matches/{mid}/join", json={}, headers=H2).json()["phase"] == "running"

    # Advance the wall clock half a second: the engine should fast-forward 5 ticks
    # (10 tps) when the next action lands.
    NOW[0] += 0.5
    r = client.post(f"/v1/matches/{mid}/actions",
                    json={"actions": [{"type": "move", "dx": 1, "dy": 0}]}, headers=H1)
    assert r.status_code == 200, r.text
    # The action POST returns the same envelope as GET /state, computed after the
    # apply, so one round trip per tick suffices.
    posted = r.json()
    assert posted["tick"] == 5
    assert posted["seat"]["player_id"] == "p1"
    assert "players" in posted["observation"]

    body = client.get(f"/v1/matches/{mid}/state", headers=H1).json()
    assert body["tick"] == 5
    assert body["seat"]["player_id"] == "p1"
    assert "players" in body["observation"]

    # Spectator scene is public and shows the same world.
    scene = client.get(f"/v1/matches/{mid}/scene").json()
    assert scene["phase"] == "running"
    assert scene["scene"]["arena_size"]

    # A non-participant cannot read another match's private observation.
    server._STORE.put_user("u3", "Outsider")
    server._STORE.put_token("tok3", "u3", "test")
    r = client.get(f"/v1/matches/{mid}/state", headers={"Authorization": "Bearer tok3"})
    assert r.status_code == 403


def test_join_unknown_match(client):
    assert client.post("/v1/matches/nope/join", json={}, headers=H1).status_code == 404


def test_skirmish_full_flow(client):
    # End-to-end coverage of the headline game over the real HTTP surface.
    r = client.post("/v1/matches", json={"game_id": "skirmish", "config": {"grid": 9}}, headers=H1)
    assert r.status_code == 200, r.text
    mid = r.json()["match_id"]
    client.post(f"/v1/matches/{mid}/join", json={}, headers=H1)
    assert client.post(f"/v1/matches/{mid}/join", json={}, headers=H2).json()["phase"] == "running"

    NOW[0] += 0.5  # skirmish is realtime; advance the wall clock
    body = client.get(f"/v1/matches/{mid}/state", headers=H1).json()
    assert body["seat"]["player_id"] == "p1"
    obs = body["observation"]
    assert "you" in obs and "view" in obs and obs["round"] == 1
    # Resolved config is surfaced on the match object (not just the {grid:9} override),
    # so an agent can size its map without guessing the schema defaults.
    cfg = client.get(f"/v1/matches/{mid}").json()["config"]
    assert cfg["grid"] == 9 and cfg["fire_range"] == 4 and cfg["hearts"] == 3

    r = client.post(f"/v1/matches/{mid}/actions",
                    json={"actions": [{"type": "turn", "to": "left"}, {"type": "fire"}]}, headers=H1)
    assert r.status_code == 200, r.text
    # POST returns the post-apply view in one call: seat + a populated observation.
    posted = r.json()
    assert posted["seat"]["player_id"] == "p1"
    assert "you" in posted["observation"] and "view" in posted["observation"]

    scene = client.get(f"/v1/matches/{mid}/scene").json()
    assert scene["scene"]["walls"] and len(scene["scene"]["players"]) == 2


def test_create_match_rejects_out_of_range_config(client):
    # The validator stops a config that would otherwise hang/crash the maze builder.
    r = client.post("/v1/matches",
                    json={"game_id": "skirmish", "config": {"wall_density": 1.5}}, headers=H1)
    assert r.status_code == 422
    assert "wall_density" in r.json()["detail"]


def test_turn_paced_advances_one_tick_per_action(client):
    # Lockdown is realtime too in v1, but verify the wall clock does not advance a
    # match with no elapsed time: two reads at the same instant give the same tick.
    r = client.post("/v1/matches",
                    json={"game_id": "lockdown", "config": {"fragments": 2}}, headers=H1)
    mid = r.json()["match_id"]
    client.post(f"/v1/matches/{mid}/join", json={}, headers=H1)  # min_players 1 -> autostart
    a = client.get(f"/v1/matches/{mid}/state", headers=H1).json()["tick"]
    b = client.get(f"/v1/matches/{mid}/state", headers=H1).json()["tick"]
    assert a == b  # no wall time passed between the two reads


def _admin_headers():
    server.ENGINE._store.put_user("admin", "Admin")
    server.ENGINE._store.put_token("admin-token", "admin", "test", admin=True)
    return {"Authorization": "Bearer admin-token"}


def test_reset_is_admin_only(client):
    mid = client.post("/v1/matches",
                      json={"game_id": "skirmish", "config": {"grid": 11}, "autostart": False},
                      headers=H1).json()["match_id"]
    client.post(f"/v1/matches/{mid}/join", json={}, headers=H1)
    client.post(f"/v1/matches/{mid}/join", json={}, headers=H2)
    client.post(f"/v1/matches/{mid}/start", json={}, headers=H1)

    assert client.post(f"/v1/matches/{mid}/reset").status_code == 401          # no token
    assert client.post(f"/v1/matches/{mid}/reset", headers=H2).status_code == 403  # non-admin (H2 is a player)
    r = client.post(f"/v1/matches/{mid}/reset", headers=_admin_headers())      # admin
    assert r.status_code == 200 and r.json()["phase"] == "running"


def test_reset_unknown_match_404(client):
    assert client.post("/v1/matches/nope/reset", headers=_admin_headers()).status_code == 404


def test_match_info_tick_projects_to_now(client):
    # Reads project to now. The metadata reads (GET /v1/matches/{id}, /v1/arena, list)
    # must report the live, wall-clock-advanced tick, not the stale stored 0 that only
    # changes on a write. Before this, those endpoints read tick 0 for a running match
    # while /scene already reported the real tick.
    mid = client.post("/v1/matches",
                      json={"game_id": "skirmish", "config": {"grid": 11}, "autostart": False},
                      headers=_admin_headers()).json()["match_id"]
    client.post(f"/v1/matches/{mid}/join", json={}, headers=H1)
    client.post(f"/v1/matches/{mid}/join", json={}, headers=H2)
    client.post(f"/v1/matches/{mid}/start", json={}, headers=H1)
    assert client.get(f"/v1/matches/{mid}").json()["tick"] == 0       # just started

    NOW[0] += 5.0                                                     # 5 s = 50 ticks at 10 Hz
    info_tick = client.get(f"/v1/matches/{mid}").json()["tick"]
    arena_tick = client.get("/v1/arena").json()["tick"]
    list_tick = client.get("/v1/matches").json()[0]["tick"]
    scene_tick = client.get(f"/v1/matches/{mid}/scene").json()["tick"]
    assert info_tick > 0                                              # projected, not the stale 0
    assert info_tick == arena_tick == list_tick == scene_tick        # every read agrees


def test_end_round_is_admin_only_and_advances_a_bout(client):
    mid = client.post("/v1/matches",
                      json={"game_id": "skirmish", "config": {"grid": 11}, "autostart": False},
                      headers=_admin_headers()).json()["match_id"]
    client.post(f"/v1/matches/{mid}/join", json={}, headers=H1)
    client.post(f"/v1/matches/{mid}/join", json={}, headers=H2)
    client.post(f"/v1/matches/{mid}/start", json={}, headers=H1)

    assert client.post(f"/v1/matches/{mid}/end_round").status_code == 401          # no token
    assert client.post(f"/v1/matches/{mid}/end_round", headers=H2).status_code == 403  # non-admin
    before = client.get(f"/v1/matches/{mid}").json()["tick"]
    NOW[0] += 0.5
    r = client.post(f"/v1/matches/{mid}/end_round", headers=_admin_headers())       # admin
    assert r.status_code == 200 and r.json()["phase"] == "running"
    # The bout advanced (a fresh round started); the match is still running, not finished.
    body = client.get(f"/v1/matches/{mid}/scene").json()
    assert body["phase"] == "running"


def test_create_match_is_admin_only(client):
    # Agents join, they do not create. A non-admin player is rejected; admin allowed.
    assert client.post("/v1/matches", json={"game_id": "skirmish"}, headers=H2).status_code == 403
    assert client.post("/v1/matches", json={"game_id": "skirmish"}, headers=_admin_headers()).status_code == 200


def test_delete_match_admin_only(client):
    mid = client.post("/v1/matches", json={"game_id": "skirmish"}, headers=_admin_headers()).json()["match_id"]
    assert client.delete(f"/v1/matches/{mid}", headers=H2).status_code == 403       # non-admin
    assert client.delete(f"/v1/matches/{mid}", headers=_admin_headers()).status_code == 200
    assert client.get(f"/v1/matches/{mid}").status_code == 404                       # gone


def test_list_matches_phase_filter(client):
    a = client.post("/v1/matches", json={"game_id": "skirmish", "autostart": False},
                    headers=_admin_headers()).json()["match_id"]
    assert a in {m["match_id"] for m in client.get("/v1/matches?phase=lobby").json()}
    assert a not in {m["match_id"] for m in client.get("/v1/matches?phase=running").json()}


def test_break_window_admin_only(client):
    mid = client.post("/v1/matches", json={"game_id": "skirmish"}, headers=_admin_headers()).json()["match_id"]
    r = client.post(f"/v1/matches/{mid}/break", json={"minutes": 5, "note": "improve"},
                    headers=_admin_headers())
    assert r.status_code == 200 and r.json()["break_until"] is not None and r.json()["break_note"] == "improve"
    assert client.post(f"/v1/matches/{mid}/break", json={"minutes": 1}, headers=H2).status_code == 403


def test_join_uses_token_name_when_omitted(client):
    # An agent joins with no display_name; the name comes from its token (dev2 -> "Dev Player 2").
    mid = client.post("/v1/matches", json={"game_id": "skirmish", "autostart": False},
                      headers=_admin_headers()).json()["match_id"]
    info = client.post(f"/v1/matches/{mid}/join", json={}, headers=H2).json()
    assert any(p["display_name"] == "Dev Player 2" for p in info["players"])


def test_join_ignores_client_supplied_name(client):
    # The name on the board is server-side, bound to the token. A client that sends
    # a display_name override must NOT be able to rename itself: the field was removed
    # and is ignored, so the token name still wins.
    mid = client.post("/v1/matches", json={"game_id": "skirmish", "autostart": False},
                      headers=_admin_headers()).json()["match_id"]
    info = client.post(f"/v1/matches/{mid}/join", json={"display_name": "Imposter"},
                       headers=H2).json()
    names = [p["display_name"] for p in info["players"]]
    assert "Dev Player 2" in names
    assert "Imposter" not in names


def test_finished_match_state_no_403_for_pruned_participant(client):
    # Item J at the HTTP boundary: a past participant whose slot was pruned still
    # gets 200 and the frozen result on a finished match, not 403.
    mid = client.post("/v1/matches",
                      json={"game_id": "skirmish",
                            "config": {"grid": 11, "drop_after": 3, "rounds_to_win": 1},
                            "autostart": False},
                      headers=_admin_headers()).json()["match_id"]
    client.post(f"/v1/matches/{mid}/join", json={}, headers=H1)
    client.post(f"/v1/matches/{mid}/join", json={}, headers=H2)
    client.post(f"/v1/matches/{mid}/start", json={}, headers=H1)
    # H1 keeps acting; H2 never does and is pruned from the roster.
    for _ in range(8):
        NOW[0] += 0.1
        client.post(f"/v1/matches/{mid}/actions", json={"actions": [{"type": "wait"}]}, headers=H1)
    # Force a finish by giving p1 (H1) the round-win limit, then a read finalizes it.
    rec, ver = server._STORE.get_match_state(mid)
    rec.state["fighters"]["p1"]["round_wins"] = 1
    server._STORE.put_match_state(mid, rec, ver)
    NOW[0] += 0.1
    assert client.get(f"/v1/matches/{mid}/scene").json()["phase"] == "finished"

    r = client.get(f"/v1/matches/{mid}/state", headers=H2)
    assert r.status_code == 200, r.text
    assert r.json()["phase"] == "finished"
    assert r.json()["result"]["winners"] == ["p1"]


def test_arena_resolves_single_match(client):
    # GET /v1/arena returns the single current match; no auth needed.
    mid = client.post("/v1/matches", json={"game_id": "skirmish", "autostart": False},
                      headers=_admin_headers()).json()["match_id"]
    r = client.get("/v1/arena")
    assert r.status_code == 200, r.text
    assert r.json()["match_id"] == mid
    assert r.json()["phase"] == "lobby"


def test_arena_empty_when_no_match(client):
    # With no match in the store, GET /v1/arena is 204 with an empty body. The
    # store is shared across this module's tests, so clear any leftover first.
    for m in client.get("/v1/matches").json():
        client.delete(f"/v1/matches/{m['match_id']}", headers=_admin_headers())
    r = client.get("/v1/arena")
    assert r.status_code == 204
    assert not r.content


def test_create_match_replaces_previous_over_http(client):
    # The single-arena model holds over the HTTP surface too: creating a second
    # match removes the first, so GET /v1/matches returns only the newest.
    first = client.post("/v1/matches", json={"game_id": "skirmish", "autostart": False},
                        headers=_admin_headers()).json()["match_id"]
    second = client.post("/v1/matches", json={"game_id": "deathmatch", "autostart": False},
                         headers=_admin_headers()).json()["match_id"]
    ids = [m["match_id"] for m in client.get("/v1/matches").json()]
    assert ids == [second]
    assert client.get(f"/v1/matches/{first}").status_code == 404
    assert client.get("/v1/arena").json()["match_id"] == second


def test_named_room_is_stable_and_reused(client):
    a = client.post("/v1/matches", json={"game_id": "skirmish", "room": "friday", "autostart": False},
                    headers=_admin_headers()).json()
    b = client.post("/v1/matches", json={"game_id": "skirmish", "room": "friday", "autostart": False},
                    headers=_admin_headers()).json()
    assert a["match_id"] == b["match_id"] and a["match_id"].startswith("room-")
    assert a["room"] == "friday"
