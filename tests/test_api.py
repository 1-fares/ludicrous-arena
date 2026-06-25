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
    assert r.status_code == 200 and r.json()["queued"] == 1
    assert r.json()["tick"] == 5

    body = client.get(f"/v1/matches/{mid}/state", headers=H1).json()
    assert body["tick"] == 5
    assert body["you"]["player_id"] == "p1"
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
    client.post(f"/v1/matches/{mid}/join", json={"display_name": "A"}, headers=H1)
    assert client.post(f"/v1/matches/{mid}/join", json={"display_name": "B"}, headers=H2).json()["phase"] == "running"

    NOW[0] += 0.5  # skirmish is realtime; advance the wall clock
    body = client.get(f"/v1/matches/{mid}/state", headers=H1).json()
    assert body["you"]["player_id"] == "p1"
    obs = body["observation"]
    assert "you" in obs and "view" in obs and obs["round"] == 1

    r = client.post(f"/v1/matches/{mid}/actions",
                    json={"actions": [{"type": "turn", "to": "left"}, {"type": "fire"}]}, headers=H1)
    assert r.status_code == 200 and r.json()["queued"] == 2

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
    client.post(f"/v1/matches/{mid}/join", json={"display_name": "Echo"}, headers=H1)
    client.post(f"/v1/matches/{mid}/join", json={"display_name": "Fox"}, headers=H2)
    client.post(f"/v1/matches/{mid}/start", json={}, headers=H1)

    assert client.post(f"/v1/matches/{mid}/reset").status_code == 401          # no token
    assert client.post(f"/v1/matches/{mid}/reset", headers=H2).status_code == 403  # non-admin (H2 is a player)
    r = client.post(f"/v1/matches/{mid}/reset", headers=_admin_headers())      # admin
    assert r.status_code == 200 and r.json()["phase"] == "running"


def test_reset_unknown_match_404(client):
    assert client.post("/v1/matches/nope/reset", headers=_admin_headers()).status_code == 404


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
