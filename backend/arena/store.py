"""Durable state. In the serverless design the store holds *everything* that must
survive between requests, because no match lives in process memory: tokens, users,
match metadata (roster, phase, result), and the serialized live match state.

Single-table Tablestore (OTS) layout (table from ``ARENA_TABLE``, default
``arena``):

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


_MATCH_TTL = 7 * 86400   # seconds; match items auto-expire if untouched this long


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


class OTSStore:
    """Tablestore (OTS) store. Same single-table pk/sk layout as the old
    DynamoDB design; the conditional version check maps to an OTS
    SingleColumnCondition. Credentials come from the FC function's STS role
    (injected as environment variables by the runtime)."""

    def __init__(self, table_name: Optional[str] = None) -> None:
        from tablestore import (
            OTSClient,
            Row,
            Condition,
            RowExistenceExpectation,
            SingleColumnCondition,
            ComparatorType,
            Direction,
            INF_MIN,
            INF_MAX,
        )
        from tablestore.error import OTSServiceError

        self._Row = Row
        self._Condition = Condition
        self._RowExistenceExpectation = RowExistenceExpectation
        self._SingleColumnCondition = SingleColumnCondition
        self._ComparatorType = ComparatorType
        self._Direction = Direction
        self._INF_MIN = INF_MIN
        self._INF_MAX = INF_MAX
        self._OTSServiceError = OTSServiceError

        # Default matches the deployed table name (terraform project_prefix "arena").
        # The FC function always gets ARENA_TABLE injected; this default is for the CLI.
        self.table_name = table_name or os.environ.get("ARENA_TABLE", "arena")

        endpoint = os.environ["OTS_ENDPOINT"]
        instance = os.environ["OTS_INSTANCE"]
        ak_id = os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"]
        ak_secret = os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"]
        sts_token = os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN")
        self._client = OTSClient(endpoint, ak_id, ak_secret, instance, sts_token=sts_token)

    # -- helpers -----------------------------------------------------------

    def _get_row(self, pk: str, sk: str) -> Optional[dict[str, Any]]:
        _, row, _ = self._client.get_row(
            self.table_name, [("pk", pk), ("sk", sk)], [])
        if row is None:
            return None
        return self._row_to_dict(row)

    def _put_row(self, pk: str, sk: str, attrs: list[tuple[str, Any]]) -> None:
        row = self._Row([("pk", pk), ("sk", sk)], attrs)
        condition = self._Condition(self._RowExistenceExpectation.IGNORE)
        self._client.put_row(self.table_name, row, condition)

    def _delete_row(self, pk: str, sk: str) -> None:
        row = self._Row([("pk", pk), ("sk", sk)])
        condition = self._Condition(self._RowExistenceExpectation.IGNORE)
        self._client.delete_row(self.table_name, row, condition)

    @staticmethod
    def _row_to_dict(row) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for name, value in row.primary_key:
            d[name] = value
        for name, value, *_ in row.attribute_columns:
            d[name] = value
        return d

    # -- Store interface ---------------------------------------------------

    def resolve_token(self, token: str) -> Optional[_Identity]:
        item = self._get_row(f"TOKEN#{hash_token(token)}", "-")
        if not item or item.get("revoked"):
            return None
        uid = item["user_id"]
        u = self._get_row(f"USER#{uid}", "-") or {}
        return _Identity(uid, u.get("display_name", uid), bool(item.get("admin")))

    def put_user(self, user_id: str, display_name: str) -> None:
        self._put_row(f"USER#{user_id}", "-",
                      [("display_name", display_name), ("created_at", int(time.time()))])

    def put_token(self, token: str, user_id: str, label: str, admin: bool = False) -> None:
        self._put_row(f"TOKEN#{hash_token(token)}", "-",
                      [("user_id", user_id), ("label", label), ("admin", admin),
                       ("revoked", False), ("created_at", int(time.time()))])

    def save_match_result(self, match_id: str, game_id: str, result: dict[str, Any]) -> None:
        self._put_row(f"MATCH#{match_id}", "RESULT",
                      [("game_id", game_id), ("result", _dumps(result)),
                       ("finished_at", int(time.time())),
                       ("ttl", int(time.time()) + _MATCH_TTL)])

    def _get(self, match_id: str, sk: str):
        item = self._get_row(f"MATCH#{match_id}", sk)
        if not item:
            return None
        return json.loads(item["data"]), int(item["version"])

    def _put(self, match_id: str, sk: str, data: dict[str, Any], expected_version):
        new_ver = (expected_version or 0) + 1
        pk = [("pk", f"MATCH#{match_id}"), ("sk", sk)]
        attrs = [("data", _dumps(data)), ("version", new_ver),
                 ("ttl", int(time.time()) + _MATCH_TTL)]
        row = self._Row(pk, attrs)
        try:
            if expected_version is None:
                # First write of this (pk, sk) row: the row must not exist yet.
                condition = self._Condition(self._RowExistenceExpectation.EXPECT_NOT_EXIST)
            else:
                # Update: the row must exist AND carry the expected version.
                condition = self._Condition(
                    self._RowExistenceExpectation.EXPECT_EXIST,
                    self._SingleColumnCondition(
                        "version", expected_version, self._ComparatorType.EQUAL))
            self._client.put_row(self.table_name, row, condition)
        except self._OTSServiceError as e:
            if e.code == "OTSConditionCheckFail":
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
        self._put_row("INDEX#MATCHES", f"MATCH#{match_id}",
                      [("game_id", game_id), ("phase", phase),
                       ("ttl", int(time.time()) + _MATCH_TTL)])

    def list_match_index(self) -> list[dict[str, Any]]:
        start_pk = [("pk", "INDEX#MATCHES"), ("sk", self._INF_MIN)]
        end_pk = [("pk", "INDEX#MATCHES"), ("sk", self._INF_MAX)]
        _, _, rows, _ = self._client.get_range(
            self.table_name, self._Direction.FORWARD, start_pk, end_pk,
            columns_to_get=[], limit=None)
        out = []
        for row in rows:
            d = self._row_to_dict(row)
            out.append({"match_id": d["sk"].split("#", 1)[1],
                        "game_id": d["game_id"], "phase": d["phase"]})
        return out

    def delete_match(self, match_id: str) -> None:
        # Remove the three per-match rows and the index pointer.
        for sk in ("META", "STATE", "RESULT"):
            self._delete_row(f"MATCH#{match_id}", sk)
        self._delete_row("INDEX#MATCHES", f"MATCH#{match_id}")


def from_env() -> Store:
    kind = os.environ.get("ARENA_STORE", "memory").lower()
    if kind == "ots":
        return OTSStore()
    store = MemoryStore()
    # Seed dev identities so local multi-agent play works out of the box.
    base = os.environ.get("ARENA_DEV_TOKEN", "dev-token")  # plus dev-token-2..4 below
    store.put_user("dev", "Dev Player")
    store.put_token(base, "dev", "local-dev", admin=True)  # local operator is admin
    for i in range(2, 5):
        store.put_user(f"dev{i}", f"Dev Player {i}")
        store.put_token(f"{base}-{i}", f"dev{i}", "local-dev")  # players, non-admin
    return store
