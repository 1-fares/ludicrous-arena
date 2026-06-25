"""Durable state. In the serverless design the store holds *everything* that must
survive between requests, because no match lives in process memory: tokens, users,
match metadata (roster, phase, result), and the serialized live match state.

Single-table DynamoDB layout (table from ``ARENA_TABLE``, default ``arena``):

    pk                       sk                attributes
    TOKEN#<sha256(token)>    -                 user_id, label, revoked, created_at
    USER#<user_id>           -                 display_name, created_at
    MATCH#<match_id>         META              data(json: MatchInfo), version
    MATCH#<match_id>         STATE             data(json: StateRecord), version
    INDEX#MATCHES            MATCH#<match_id>   game_id, phase   (for listing)

META and STATE each carry their own ``version`` integer; writes are conditional on
it (optimistic concurrency), which serializes the concurrent agents of one match
without a lock. ``Conflict`` is raised on a version mismatch so the engine can
retry. ``MemoryStore`` mirrors the same semantics in-process for dev and tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Protocol


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _dumps(obj: Any) -> str:
    """Serialize to JSON, rejecting non-finite floats. ``allow_nan=False`` turns a
    stray NaN/inf (a game bug) into a clean ValueError at write time instead of
    emitting the bare ``NaN`` token, which is invalid JSON that a strict external
    client could not parse."""
    return json.dumps(obj, allow_nan=False)


class Conflict(Exception):
    """Raised when a conditional write loses the version check. The engine retries."""


@dataclass
class StateRecord:
    """The serialized live state of one match, plus the bookkeeping the engine
    needs to fast-forward the simulation to 'now'."""

    state: dict[str, Any]                                   # game.encode_state output
    last_tick: int                                         # tick the state is materialized at
    last_wall_ms: int                                      # wall time of last_tick (epoch ms)
    rejected: dict[str, list[str]] = field(default_factory=dict)  # per-player, last submission


class _Identity:
    __slots__ = ("user_id", "display_name", "is_admin")

    def __init__(self, user_id: str, display_name: str, is_admin: bool = False):
        self.user_id = user_id
        self.display_name = display_name
        self.is_admin = is_admin


class Store(Protocol):
    def resolve_token(self, token: str) -> Optional[_Identity]: ...
    def put_user(self, user_id: str, display_name: str) -> None: ...
    def put_token(self, token: str, user_id: str, label: str, admin: bool = False) -> None: ...
    def save_match_result(self, match_id: str, game_id: str, result: dict[str, Any]) -> None: ...

    # match metadata (roster, phase, config, result)
    def get_match_meta(self, match_id: str) -> Optional[tuple[dict[str, Any], int]]: ...
    def put_match_meta(self, match_id: str, data: dict[str, Any], expected_version: Optional[int]) -> int: ...

    # serialized live state
    def get_match_state(self, match_id: str) -> Optional[tuple[StateRecord, int]]: ...
    def put_match_state(self, match_id: str, rec: StateRecord, expected_version: Optional[int]) -> int: ...

    # listing
    def update_match_index(self, match_id: str, game_id: str, phase: str) -> None: ...
    def list_match_index(self) -> list[dict[str, Any]]: ...

    # removal (admin cleanup)
    def delete_match(self, match_id: str) -> None: ...


class MemoryStore:
    """In-process store with the same versioning semantics as DynamoStore."""

    def __init__(self) -> None:
        self._tokens: dict[str, dict[str, Any]] = {}
        self._users: dict[str, str] = {}
        self._results: dict[str, dict[str, Any]] = {}
        self._meta: dict[str, tuple[dict[str, Any], int]] = {}
        self._state: dict[str, tuple[StateRecord, int]] = {}
        self._index: dict[str, dict[str, Any]] = {}

    def resolve_token(self, token: str) -> Optional[_Identity]:
        rec = self._tokens.get(hash_token(token))
        if not rec or rec.get("revoked"):
            return None
        uid = rec["user_id"]
        return _Identity(uid, self._users.get(uid, uid), bool(rec.get("admin")))

    def put_user(self, user_id: str, display_name: str) -> None:
        self._users[user_id] = display_name

    def put_token(self, token: str, user_id: str, label: str, admin: bool = False) -> None:
        self._tokens[hash_token(token)] = {"user_id": user_id, "label": label, "admin": admin,
                                           "revoked": False, "created_at": int(time.time())}

    def save_match_result(self, match_id: str, game_id: str, result: dict[str, Any]) -> None:
        self._results[match_id] = {"game_id": game_id, "result": result}

    def get_match_meta(self, match_id: str):
        rec = self._meta.get(match_id)
        if rec is None:
            return None
        data, ver = rec
        return json.loads(_dumps(data)), ver  # deep copy so callers cannot alias

    def put_match_meta(self, match_id: str, data: dict[str, Any], expected_version):
        cur = self._meta.get(match_id)
        cur_ver = cur[1] if cur else None
        if expected_version != cur_ver:
            raise Conflict(f"meta version: expected {expected_version}, have {cur_ver}")
        new_ver = (expected_version or 0) + 1
        self._meta[match_id] = (json.loads(_dumps(data)), new_ver)
        return new_ver

    def get_match_state(self, match_id: str):
        rec = self._state.get(match_id)
        if rec is None:
            return None
        sr, ver = rec
        return StateRecord(**json.loads(_dumps(asdict(sr)))), ver

    def put_match_state(self, match_id: str, rec: StateRecord, expected_version):
        cur = self._state.get(match_id)
        cur_ver = cur[1] if cur else None
        if expected_version != cur_ver:
            raise Conflict(f"state version: expected {expected_version}, have {cur_ver}")
        new_ver = (expected_version or 0) + 1
        self._state[match_id] = (StateRecord(**json.loads(_dumps(asdict(rec)))), new_ver)
        return new_ver

    def update_match_index(self, match_id: str, game_id: str, phase: str) -> None:
        self._index[match_id] = {"match_id": match_id, "game_id": game_id, "phase": phase}

    def list_match_index(self) -> list[dict[str, Any]]:
        return list(self._index.values())

    def delete_match(self, match_id: str) -> None:
        self._meta.pop(match_id, None)
        self._state.pop(match_id, None)
        self._results.pop(match_id, None)
        self._index.pop(match_id, None)


class DynamoStore:
    def __init__(self, table_name: Optional[str] = None) -> None:
        import boto3
        from botocore.exceptions import ClientError

        self._ClientError = ClientError
        # Default matches the deployed table name (terraform project_prefix "arena").
        # The Lambda always gets ARENA_TABLE injected; this default is for the CLI.
        self.table_name = table_name or os.environ.get("ARENA_TABLE", "arena")
        self._table = boto3.resource("dynamodb").Table(self.table_name)

    def resolve_token(self, token: str) -> Optional[_Identity]:
        item = self._table.get_item(Key={"pk": f"TOKEN#{hash_token(token)}", "sk": "-"}).get("Item")
        if not item or item.get("revoked"):
            return None
        uid = item["user_id"]
        u = self._table.get_item(Key={"pk": f"USER#{uid}", "sk": "-"}).get("Item") or {}
        return _Identity(uid, u.get("display_name", uid), bool(item.get("admin")))

    def put_user(self, user_id: str, display_name: str) -> None:
        self._table.put_item(Item={"pk": f"USER#{user_id}", "sk": "-",
                                   "display_name": display_name, "created_at": int(time.time())})

    def put_token(self, token: str, user_id: str, label: str, admin: bool = False) -> None:
        self._table.put_item(Item={"pk": f"TOKEN#{hash_token(token)}", "sk": "-",
                                   "user_id": user_id, "label": label, "admin": admin,
                                   "revoked": False, "created_at": int(time.time())})

    def save_match_result(self, match_id: str, game_id: str, result: dict[str, Any]) -> None:
        self._table.put_item(Item={"pk": f"MATCH#{match_id}", "sk": "RESULT",
                                   "game_id": game_id, "result": _dumps(result),
                                   "finished_at": int(time.time())})

    def _get(self, match_id: str, sk: str):
        item = self._table.get_item(Key={"pk": f"MATCH#{match_id}", "sk": sk}).get("Item")
        if not item:
            return None
        return json.loads(item["data"]), int(item["version"])

    def _put(self, match_id: str, sk: str, data: dict[str, Any], expected_version):
        new_ver = (expected_version or 0) + 1
        item = {"pk": f"MATCH#{match_id}", "sk": sk, "data": _dumps(data), "version": new_ver}
        try:
            if expected_version is None:
                # First write of this (pk, sk) item. The condition is evaluated
                # against the specific item, not the partition, so checking sk is
                # the clearer "this item does not exist yet" guard (META for the
                # same pk may already exist when STATE is first written).
                self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(sk)")
            else:
                self._table.put_item(Item=item,
                                     ConditionExpression="version = :v",
                                     ExpressionAttributeValues={":v": expected_version})
        except self._ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise Conflict(f"{sk} version mismatch (expected {expected_version})") from None
            raise
        return new_ver

    def get_match_meta(self, match_id: str):
        return self._get(match_id, "META")

    def put_match_meta(self, match_id: str, data: dict[str, Any], expected_version):
        return self._put(match_id, "META", data, expected_version)

    def get_match_state(self, match_id: str):
        got = self._get(match_id, "STATE")
        if got is None:
            return None
        data, ver = got
        return StateRecord(**data), ver

    def put_match_state(self, match_id: str, rec: StateRecord, expected_version):
        return self._put(match_id, "STATE", asdict(rec), expected_version)

    def update_match_index(self, match_id: str, game_id: str, phase: str) -> None:
        self._table.put_item(Item={"pk": "INDEX#MATCHES", "sk": f"MATCH#{match_id}",
                                   "game_id": game_id, "phase": phase})

    def list_match_index(self) -> list[dict[str, Any]]:
        resp = self._table.query(
            KeyConditionExpression="pk = :p",
            ExpressionAttributeValues={":p": "INDEX#MATCHES"})
        return [{"match_id": i["sk"].split("#", 1)[1], "game_id": i["game_id"], "phase": i["phase"]}
                for i in resp.get("Items", [])]

    def delete_match(self, match_id: str) -> None:
        # Remove the three per-match items and the index pointer.
        for sk in ("META", "STATE", "RESULT"):
            self._table.delete_item(Key={"pk": f"MATCH#{match_id}", "sk": sk})
        self._table.delete_item(Key={"pk": "INDEX#MATCHES", "sk": f"MATCH#{match_id}"})


def from_env() -> Store:
    kind = os.environ.get("ARENA_STORE", "memory").lower()
    if kind == "dynamo":
        return DynamoStore()
    store = MemoryStore()
    # Seed dev identities so local multi-agent play works out of the box.
    base = os.environ.get("ARENA_DEV_TOKEN", "dev-token")  # plus dev-token-2..4 below
    store.put_user("dev", "Dev Player")
    store.put_token(base, "dev", "local-dev", admin=True)  # local operator is admin
    for i in range(2, 5):
        store.put_user(f"dev{i}", f"Dev Player {i}")
        store.put_token(f"{base}-{i}", f"dev{i}", "local-dev")  # players, non-admin
    return store
