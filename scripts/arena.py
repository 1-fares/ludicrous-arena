#!/usr/bin/env python3
"""arena: the operator CLI for running Ludicrous Arena games.

One tool for the whole flow, no curl, no DynamoDB surgery:

    arena mint alice bob carol           # mint a player token per name (name lives in the token)
    arena mint --admin                   # mint (and cache) an admin token
    arena create                         # admin-create one skirmish match (autostart off)
    arena brief                          # print a ready-to-paste briefing per player (token only)
    arena start --at 5                   # start once 5 players have joined
    arena ls                             # list matches and their phase
    arena status                         # roster + live scores for the current match
    arena break --minutes 5 --note "..." # open an intermission so agents can improve
    arena reset                          # start the next round (same match, scores wiped)
    arena clean --finished               # delete finished matches
    arena down                           # delete the current match and forget it

State lives in ~/.arena/ledger.json (api base, current match id, minted tokens, admin
token), so brief/start/reset/etc. need no arguments after create. Player tokens carry
the player's name; agents are handed only a token. Minting needs boto3 + AWS creds
(same as scripts/issue-token.py); everything else is plain HTTP with the admin token.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-2")  # boto3 region for minting
LEDGER = Path(os.environ.get("ARENA_HOME", Path.home() / ".arena")) / "ledger.json"
DEFAULT_API = os.environ.get("ARENA_API", "https://api.ludicrous-arena.com")
VIEWER = os.environ.get("ARENA_VIEWER", "https://ludicrous-arena.com")


def _load():
    if LEDGER.exists():
        return json.loads(LEDGER.read_text())
    return {"api": DEFAULT_API, "match": None, "players": {}, "admin": None}


def _save(led):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(led, indent=2))


def _req(led, method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(led["api"] + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")
        raise SystemExit(f"error {e.code} {method} {path}: {msg}")


def _admin(led):
    tok = os.environ.get("ARENA_ADMIN_TOKEN") or led.get("admin")
    if not tok:
        raise SystemExit("no admin token. Run: arena mint --admin")
    return tok


def _match(led, override=None):
    mid = override or led.get("match")
    if not mid:
        raise SystemExit("no current match. Run: arena create")
    return mid


# -- commands ---------------------------------------------------------------

def cmd_mint(led, args):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    import secrets
    from arena.store import DynamoStore  # noqa: E402
    store = DynamoStore(table_name=os.environ.get("ARENA_TABLE"))

    def issue(user_id, name, admin):
        token = "arena_" + secrets.token_urlsafe(32)
        store.put_user(user_id, name)
        store.put_token(token, user_id, "arena-cli", admin=admin)
        return token

    if args.admin:
        led["admin"] = issue("admin", "Admin", True)
        print(f"admin token: {led['admin']}")
    for name in args.names:
        uid = name.lower()
        led["players"][name] = issue(uid, name, False)
        print(f"{name:12} {led['players'][name]}")
    _save(led)
    if args.names:
        print("\nHand each player only their token. The name is bound to the token.")


def cmd_create(led, args):
    cfg = json.loads(args.config) if args.config else {"score_to_win": args.score}
    body = {"game_id": args.game, "config": cfg, "autostart": False}
    if args.room:
        body["room"] = args.room
    info = _req(led, "POST", "/v1/matches", body, _admin(led))
    led["match"] = info["match_id"]
    led["room"] = args.room
    _save(led)
    print(f"match: {info['match_id']}  ({args.game}{', room ' + args.room if args.room else ''})")
    print(f"watch: {VIEWER}/?match={info['match_id']}")


def cmd_brief(led, args):
    mid = _match(led, args.match)
    if not led["players"]:
        raise SystemExit("no minted players. Run: arena mint <names...>")
    for name, token in led["players"].items():
        print("=" * 64)
        print(f"# Briefing for {name} (hand them ONLY the lines below)\n")
        print(f"API base : {led['api']}")
        print(f"Token    : {token}")
        print(f"Match    : {mid}")
        print("Play     : join (empty body), wait for phase==running, then loop")
        print("           GET /v1/matches/%s/state -> POST /v1/matches/%s/actions" % (mid, mid))
        print("           Stay resident: on 'finished' keep polling; resume on reset.")
        print(f"Docs     : {VIEWER.replace('//', '//docs.')}  ·  rules+schemas: {led['api']}/v1/games")
        print(f"Client   : examples/python-agent/skirmish_bot.py --api {led['api']} --token <TOKEN>")
    print("=" * 64)


def cmd_start(led, args):
    mid = _match(led, args.match)
    deadline = time.time() + args.wait
    while time.time() < deadline:
        n = len(_req(led, "GET", f"/v1/matches/{mid}")["players"])
        print(f"  {n}/{args.at} joined", flush=True)
        if n >= args.at:
            break
        time.sleep(2)
    _req(led, "POST", f"/v1/matches/{mid}/start", {}, _admin(led))
    print(f"started {mid}")


def cmd_reset(led, args):
    mid = _match(led, args.match)
    _req(led, "POST", f"/v1/matches/{mid}/reset", {}, _admin(led))
    print(f"reset {mid} -> fresh round 1")


def cmd_break(led, args):
    mid = _match(led, args.match)
    info = _req(led, "POST", f"/v1/matches/{mid}/break",
                {"minutes": args.minutes, "note": args.note}, _admin(led))
    print(f"break on {mid} for {args.minutes} min" + (f": {args.note}" if args.note else ""))
    print("  agents keep polling and can improve; run 'arena reset' to start the next round.")
    return info


def cmd_ls(led, args):
    qs = f"?phase={args.phase}" if args.phase else ""
    for m in _req(led, "GET", "/v1/matches" + qs):
        cur = " *" if m["match_id"] == led.get("match") else "  "
        print(f"{cur}{m['match_id'][:8]}  {m['game_id']:10} {m['phase']:9} {len(m['players'])} players")


def cmd_status(led, args):
    mid = _match(led, args.match)
    info = _req(led, "GET", f"/v1/matches/{mid}")
    scene = _req(led, "GET", f"/v1/matches/{mid}/scene")
    print(f"{mid}  {info['phase']}  round {(scene.get('scene') or {}).get('round', '-')}")
    for p in info["players"]:
        print(f"  {p['display_name']}")
    s = scene.get("scene") or {}
    if s.get("players"):
        print("scores:", {p["name"]: p["score"] for p in sorted(s["players"], key=lambda p: -p["score"])})


def cmd_clean(led, args):
    matches = _req(led, "GET", "/v1/matches")
    targets = [m for m in matches if args.all or m["phase"] == "finished"]
    keep = led.get("match")
    for m in targets:
        if m["match_id"] == keep and not args.all:
            continue
        _req(led, "DELETE", f"/v1/matches/{m['match_id']}", token=_admin(led))
        print(f"deleted {m['match_id'][:8]} ({m['phase']})")
    print(f"cleaned {len(targets)} match(es)")


def cmd_down(led, args):
    mid = _match(led, args.match)
    _req(led, "DELETE", f"/v1/matches/{mid}", token=_admin(led))
    if led.get("match") == mid:
        led["match"] = None
        _save(led)
    print(f"deleted {mid}")


def main():
    ap = argparse.ArgumentParser(prog="arena", description="Operator CLI for Ludicrous Arena.")
    ap.add_argument("--api", help="API base (overrides the ledger / $ARENA_API)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("mint", help="mint player and/or admin tokens")
    p.add_argument("names", nargs="*", help="player names (one token each)")
    p.add_argument("--admin", action="store_true", help="also mint+cache an admin token")
    p.set_defaults(fn=cmd_mint)

    p = sub.add_parser("create", help="admin-create one match (autostart off)")
    p.add_argument("--game", default="skirmish")
    p.add_argument("--room", help="stable room name (deterministic, reusable match id)")
    p.add_argument("--score", type=int, default=10, help="score_to_win (ignored if --config)")
    p.add_argument("--config", help="full config JSON (overrides --score)")
    p.set_defaults(fn=cmd_create)

    p = sub.add_parser("brief", help="print a per-player briefing (token only)")
    p.add_argument("--match")
    p.set_defaults(fn=cmd_brief)

    p = sub.add_parser("start", help="start once N players have joined")
    p.add_argument("--at", type=int, default=2, help="player count to start at")
    p.add_argument("--wait", type=float, default=300.0, help="seconds to wait for joins")
    p.add_argument("--match")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("reset", help="start the next round (same match, scores wiped)")
    p.add_argument("--match")
    p.set_defaults(fn=cmd_reset)

    p = sub.add_parser("break", help="open an intermission so agents can improve")
    p.add_argument("--minutes", type=float, default=5.0)
    p.add_argument("--note")
    p.add_argument("--match")
    p.set_defaults(fn=cmd_break)

    p = sub.add_parser("ls", help="list matches")
    p.add_argument("--phase", help="filter, e.g. running,lobby")
    p.set_defaults(fn=cmd_ls)

    p = sub.add_parser("status", help="roster + live scores for a match")
    p.add_argument("--match")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("clean", help="delete finished (or --all) matches")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_clean)

    p = sub.add_parser("down", help="delete the current match and forget it")
    p.add_argument("--match")
    p.set_defaults(fn=cmd_down)

    args = ap.parse_args()
    led = _load()
    if args.api:
        led["api"] = args.api
    args.fn(led, args)


if __name__ == "__main__":
    main()
