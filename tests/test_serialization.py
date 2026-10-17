import json
from datetime import datetime

from tooldistill.core.serialization import (
    canonical_json,
    code_hash,
    is_json_serializable,
    now_iso,
    schema_hash,
    strip_descriptions,
    to_content,
)


def test_canonical_json_sorts_keys_and_is_compact():
    assert canonical_json({"b": 1, "a": [1, {"z": None, "y": "é"}]}) == '{"a":[1,{"y":"é","z":null}],"b":1}'


def test_canonical_json_is_stable_under_key_order():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_is_json_serializable():
    assert is_json_serializable({"a": [1, 2.5, None, True]})
    assert not is_json_serializable({"a": object()})
    assert not is_json_serializable({1, 2})


def test_to_content_passes_strings_and_dumps_everything_else():
    assert to_content("plain") == "plain"
    assert to_content({"a": 1}) == json.dumps({"a": 1})
    assert to_content([1, "x"]) == '[1, "x"]'
    assert to_content(None) == "null"
    assert to_content(3) == "3"


def test_code_hash_is_sha256_hex():
    digest = code_hash("print(1)\n")
    assert len(digest) == 64 and int(digest, 16)
    assert digest != code_hash("print(2)\n")


def test_now_iso_is_utc_with_milliseconds():
    stamp = now_iso()
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
    assert stamp.endswith("+00:00") and "." in stamp


SCHEMA = {
    "type": "object",
    "description": "Search mail",
    "properties": {
        "query": {"type": "string", "description": "Gmail query"},
        "description": {"type": "string", "description": "A property that happens to be called description"},
        "filters": {
            "type": "object",
            "properties": {"label": {"type": "string", "description": "nested"}},
            "additionalProperties": {"type": "string", "description": "extra"},
        },
        "tags": {"type": "array", "items": {"type": "string", "description": "a tag"}},
        "mode": {"anyOf": [{"type": "string", "description": "a"}, {"type": "null"}]},
    },
    "required": ["query"],
}


def test_strip_descriptions_drops_only_schema_keywords():
    stripped = strip_descriptions(SCHEMA)
    assert "description" not in stripped
    assert "description" not in stripped["properties"]["query"]
    assert "description" in stripped["properties"]  # the property named "description" survives
    assert "description" not in stripped["properties"]["description"]
    assert "description" not in stripped["properties"]["filters"]["properties"]["label"]
    assert "description" not in stripped["properties"]["filters"]["additionalProperties"]
    assert "description" not in stripped["properties"]["tags"]["items"]
    assert "description" not in stripped["properties"]["mode"]["anyOf"][0]
    assert SCHEMA["properties"]["query"]["description"] == "Gmail query"  # input untouched


def test_schema_hash_ignores_descriptions_and_key_order():
    base = schema_hash(SCHEMA)
    reworded = json.loads(json.dumps(SCHEMA))
    reworded["description"] = "Different"
    reworded["properties"]["query"]["description"] = "Something else"
    reworded["properties"]["filters"]["properties"]["label"]["description"] = "changed"
    assert schema_hash(reworded) == base
    reordered = {
        "required": ["query"],
        "properties": dict(reversed(list(SCHEMA["properties"].items()))),
        "type": "object",
    }
    assert schema_hash(reordered) == base


def test_schema_hash_changes_with_structure():
    base = schema_hash(SCHEMA)
    with_currency = json.loads(json.dumps(SCHEMA))
    with_currency["properties"]["currency"] = {"type": "string"}
    with_currency["required"].append("currency")
    assert schema_hash(with_currency) != base
    retyped_prop = json.loads(json.dumps(SCHEMA))
    retyped_prop["properties"]["description"]["type"] = "integer"
    assert schema_hash(retyped_prop) != base
    assert len(base) == 64
