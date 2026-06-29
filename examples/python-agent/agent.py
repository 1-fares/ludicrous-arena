#!/usr/bin/env python3
"""Minimal protocol example for Ludicrous Arena. Zero dependencies (stdlib only)
so it is easy to read end to end. It plays `deathmatch`: each tick it reads its
observation, moves toward the nearest opponent, and fires at them.

This is the simplest possible walkthrough of the wire contract. The arena does
not care how you decide; it only sees your actions on the wire. For the headline
game and the production play loop, read `skirmish_bot.py` instead: it is the
resident, join-only reference client for the single-arena model (it never
creates a match, re-joins on reset, and plays rounds forever).

`--create` calls `POST /v1/matches`, which is **admin-only**. It works here only
because the local `dev-token` is the admin token; a normal player token gets 403
and must `--match <id>` into a match the admin already provisioned. See
../../docs/API.md (single-arena model: agents join, they do not create).

Usage:
    # Local self-contained demo: the admin dev-token creates a match and plays it
    # (waits for a second player to join):
    python agent.py --token dev-token --create

    # Join an existing match by id (the normal path for a player token):
    python agent.py --token dev-token-2 --match <match_id>

    # Point at a deployed arena (join a match the admin provisioned):
    python agent.py --api https://api.ludicrous-arena.com --token <your-token> --match <match_id>
"""

import argparse
import json
import math
import time
import urllib.error
import urllib.request


class ArenaClient:
    """Thin wrapper over the HTTP API. Every call carries the bearer token."""

    def __init__(self, api: str, token: str):
        self.api = api.rstrip("/")
        self.token = token

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.api + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read() or "{}")

    def games(self) -> list:
        return self._req("GET", "/v1/games")

    def create_match(self, game_id: str, config: dict, autostart: bool = True) -> dict:
        return self._req("POST", "/v1/matches",
                         {"game_id": game_id, "config": config, "autostart": autostart})

    def join(self, match_id: str) -> dict:
        # No name in the body: the server uses the name bound to your token.
        return self._req("POST", f"/v1/matches/{match_id}/join", {})

    def match(self, match_id: str) -> dict:
        return self._req("GET", f"/v1/matches/{match_id}")

    def state(self, match_id: str) -> dict:
        return self._req("GET", f"/v1/matches/{match_id}/state")

    def act(self, match_id: str, actions: list) -> dict:
        return self._req("POST", f"/v1/matches/{match_id}/actions", {"actions": actions})


def decide(obs: dict, me_id: str) -> list:
    """Pure strategy: return the actions to submit this tick. Replace this body
    with an LLM call, a planner, or anything else -- the wire contract is the same."""
    players = obs.get("players", [])
    me = next((p for p in players if p["id"] == me_id), None)
    if not me or not me.get("alive", True):
        return []
    enemies = [p for p in players if p["id"] != me_id and p.get("alive", True)]
    if not enemies:
        return []
    target = min(enemies, key=lambda e: (e["x"] - me["x"]) ** 2 + (e["y"] - me["y"]) ** 2)
    dx, dy = target["x"] - me["x"], target["y"] - me["y"]
    angle = math.atan2(dy, dx)
    dist = math.hypot(dx, dy)
    actions = [{"type": "move", "dx": dx, "dy": dy}]
    if dist < 8.0:  # only shoot when there is a realistic chance to connect
        actions.append({"type": "fire", "angle": angle})
    return actions


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8080")
    ap.add_argument("--token", required=True)
    ap.add_argument("--match", help="join this match id")
    ap.add_argument("--create", action="store_true", help="create a new deathmatch")
    args = ap.parse_args()

    client = ArenaClient(args.api, args.token)

    if args.create:
        match = client.create_match("deathmatch", {"score_limit": 10})
        match_id = match["match_id"]
        print(f"created match {match_id}; waiting for an opponent to join...")
    elif args.match:
        match_id = args.match
    else:
        ap.error("pass --create or --match <id>")

    client.join(match_id)
    print(f"joined {match_id}; waiting for the match to start...")

    # Wait for the match to start (deathmatch needs 2 players).
    while client.match(match_id)["phase"] == "lobby":
        time.sleep(0.5)

    # Learn our own player_id from the state endpoint -- it is keyed to our token,
    # so it does not depend on display names or join order.
    me_id = client.state(match_id)["seat"]["player_id"]
    print(f"match running, we are {me_id}")

    last_tick = -1
    while True:
        try:
            obs = client.state(match_id)
        except urllib.error.HTTPError as e:
            if e.code == 409:  # not started yet
                time.sleep(0.2)
                continue
            raise
        if obs["phase"] == "finished":
            res = obs.get("result") or {}
            won = me_id in (res.get("winners") or [])
            print(f"match over: {'WON' if won else 'lost'} ({res.get('reason')})")
            return
        if obs["seat"].get("rejected"):
            print("rejected:", obs["seat"]["rejected"])
        if obs["tick"] != last_tick:
            last_tick = obs["tick"]
            actions = decide(obs["observation"], me_id)
            if actions:
                client.act(match_id, actions)
        time.sleep(0.08)  # poll a little faster than the 10 tps tick rate


if __name__ == "__main__":
    main()
