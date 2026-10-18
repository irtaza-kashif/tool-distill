"""Canonical JSON, schema hashing and the few string conversions the whole library shares."""

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "canonical_json",
    "code_hash",
    "is_json_serializable",
    "now_iso",
    "schema_hash",
    "strip_descriptions",
    "to_content",
]


def canonical_json(obj: Any) -> str:
    """The one canonical JSON form used for storage, fixture matching and schema hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def is_json_serializable(obj: Any) -> bool:
    try:
        canonical_json(obj)
    except (TypeError, ValueError):
        return False
    return True


def to_content(value: Any) -> str:
    """``ToolResult.content`` is always a string: strings pass through, everything else is ``json.dumps``."""
    if isinstance(value, str):
        return value
    return json.dumps(value)


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def now_iso() -> str:
    """UTC timestamp with millisecond precision; sorts lexicographically."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


_ID_LOCK = threading.Lock()
_ID_STATE = [0, 0]  # millisecond, counter


def new_trace_id() -> str:
    """A 32-character hex id in the UUIDv7 layout: a millisecond timestamp first, then a counter, then random bits.

    Ids therefore sort by creation time, and two ids made in the same millisecond keep their order (the counter
    grows). ``uuid.uuid7()`` would do the same but exists only from Python 3.14.
    """
    with _ID_LOCK:
        millis = time.time_ns() // 1_000_000
        if millis > _ID_STATE[0]:
            _ID_STATE[0], _ID_STATE[1] = millis, secrets.randbits(11)  # fresh millisecond: random start, headroom above
        else:
            _ID_STATE[1] += 1  # same (or earlier, after a clock step) millisecond: strictly after the previous id
            if _ID_STATE[1] >= 1 << 12:
                _ID_STATE[0], _ID_STATE[1] = _ID_STATE[0] + 1, 0
        millis, counter = _ID_STATE
    value = (millis << 80) | (0x7 << 76) | (counter << 64) | (0b10 << 62) | secrets.randbits(62)
    return f"{value:032x}"


# JSON Schema keywords whose value is a map of *names* to schemas (keys must not be touched).
_MAP_OF_SCHEMAS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
# Keywords whose value is a list of schemas.
_LIST_OF_SCHEMAS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
# Keywords whose value is a single schema (or, for ``items``/``additionalItems``, possibly a list).
_SINGLE_SCHEMA = frozenset(
    {
        "items",
        "additionalItems",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "not",
        "if",
        "then",
        "else",
        "contains",
        "propertyNames",
    }
)


def strip_descriptions(schema: Any) -> Any:
    """Return a copy of a JSON Schema with every ``description`` keyword removed.

    Only schema keywords are dropped: a *property* named ``description`` (a key inside
    ``properties``) is data and is kept.
    """
    if not isinstance(schema, Mapping):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "description" and isinstance(value, str):
            continue
        if key in _MAP_OF_SCHEMAS and isinstance(value, Mapping):
            out[key] = {name: strip_descriptions(sub) for name, sub in value.items()}
        elif key in _LIST_OF_SCHEMAS and isinstance(value, list):
            out[key] = [strip_descriptions(sub) for sub in value]
        elif key in _SINGLE_SCHEMA:
            out[key] = (
                [strip_descriptions(sub) for sub in value] if isinstance(value, list) else strip_descriptions(value)
            )
        elif key == "dependencies" and isinstance(value, Mapping):  # draft-07: name -> schema | list of names
            out[key] = {name: (strip_descriptions(sub) if isinstance(sub, Mapping) else sub) for name, sub in value.items()}
        else:
            out[key] = value
    return out


def schema_hash(input_schema: Mapping[str, Any]) -> str:
    """SHA-256 of the canonicalized ``input_schema``; description changes do not change the hash."""
    return hashlib.sha256(canonical_json(strip_descriptions(input_schema)).encode("utf-8")).hexdigest()
