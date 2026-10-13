"""Tests for toolproto.parser: envelopes, invariants, smuggling, observations."""

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from toolproto.parser import (
    TOOL_REGISTRY,
    parse,
    validate_observation,
)

FIXTURES = Path(__file__).resolve().parent.parent / "toolproto" / "fixtures"


def read_fixture(name: str) -> str:
    """Read a fixture file as text."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def read_obs(name: str) -> Dict[str, Any]:
    """Read a JSON observation fixture."""
    return json.loads(read_fixture(name))


def tool_call(payload: str) -> str:
    """Wrap a JSON payload in a TOOL_CALL envelope."""
    return "<<<TOOL_CALL>>>\n" + payload + "\n<<<END>>>"


# Valid envelopes -----------------------------------------------------------


def test_valid_sql_fixture() -> None:
    result = parse(read_fixture("valid_sql.txt"))
    assert result.error is None, result.error
    assert result.kind == "SQL"
    assert result.payload == "\nSELECT name, email FROM customers WHERE city = 'Winter Park';\n"
    assert result.raw is not None
    assert result.raw.startswith("<<<SQL>>>")
    assert result.raw.endswith("<<<END>>>")


def test_valid_clarify_fixture() -> None:
    result = parse(read_fixture("valid_clarify.txt"))
    assert result.error is None, result.error
    assert result.kind == "CLARIFY"
    assert "retail or wholesale" in result.payload


def test_valid_unanswerable_fixture() -> None:
    result = parse(read_fixture("valid_unanswerable.txt"))
    assert result.error is None, result.error
    assert result.kind == "UNANSWERABLE"


def test_sql_with_surrounding_prose() -> None:
    text = "Here is the query:\n" + read_fixture("valid_sql.txt") + "Let me know.\n"
    result = parse(text)
    assert result.error is None, result.error
    assert result.kind == "SQL"


@pytest.mark.parametrize(
    "fixture,tool,args",
    [
        ("valid_toolcall_schema_inspect.txt", "schema.inspect", {"scope": "public"}),
        ("valid_toolcall_clock_now.txt", "clock.now", {}),
        ("valid_toolcall_sql_explain.txt", "sql.explain", {"sql": "SELECT name FROM customers"}),
        ("valid_toolcall_sql_execute.txt", "sql.execute", {"sql": "SELECT name FROM customers", "limit": 25}),
        ("valid_toolcall_nedb_provenance.txt", "nedb.provenance", {"table": "customers", "row_id": 42}),
    ],
)
def test_valid_toolcall_fixtures(fixture: str, tool: str, args: Dict[str, Any]) -> None:
    result = parse(read_fixture(fixture))
    assert result.error is None, result.error
    assert result.kind == "TOOL_CALL"
    assert isinstance(result.payload, dict)
    assert result.payload["name"] == tool
    assert result.payload["arguments"] == args
    assert result.raw is not None
    assert result.raw.startswith("<<<TOOL_CALL>>>")


def test_registry_lists_v1_tools() -> None:
    assert set(TOOL_REGISTRY) == {
        "schema.inspect",
        "clock.now",
        "sql.explain",
        "sql.execute",
        "nedb.provenance",
    }


# Protocol invariants: exactly one block or an error --------------------------


@pytest.mark.parametrize(
    "fixture,needle",
    [
        ("zero_envelopes.txt", "no envelope"),
        ("multiple_envelopes.txt", "multiple"),
        ("truncated.txt", "unterminated"),
        ("toolcall_malformed_json.txt", "JSON"),
        ("toolcall_unknown_tool.txt", "unknown tool"),
        ("toolcall_bad_arguments.txt", "missing required argument"),
        ("smuggle_end_in_sql.txt", "stray"),
        ("smuggle_end_in_toolcall.txt", "smuggled"),
    ],
)
def test_error_fixtures(fixture: str, needle: str) -> None:
    result = parse(read_fixture(fixture))
    assert result.error is not None
    assert needle in result.error
    assert result.payload is None


def test_truncated_toolcall_yields_nothing() -> None:
    result = parse('<<<TOOL_CALL>>>\n{"name": "clock.now", "arguments": {}}')
    assert result.error is not None
    assert result.payload is None


def test_multiple_envelopes_mixed_kinds() -> None:
    text = read_fixture("valid_sql.txt") + read_fixture("valid_toolcall_clock_now.txt")
    result = parse(text)
    assert result.error is not None
    assert "multiple" in result.error


def test_toolcall_wrong_argument_type() -> None:
    result = parse(tool_call('{"name": "sql.execute", "arguments": {"sql": 123}}'))
    assert result.error is not None
    assert "must be string" in result.error


def test_toolcall_unknown_argument() -> None:
    result = parse(tool_call('{"name": "clock.now", "arguments": {"bogus": true}}'))
    assert result.error is not None
    assert "unknown argument" in result.error


def test_toolcall_extra_top_level_field() -> None:
    result = parse(tool_call('{"name": "clock.now", "arguments": {}, "id": "x"}'))
    assert result.error is not None
    assert "unknown fields" in result.error


def test_toolcall_non_object_payload() -> None:
    result = parse(tool_call('["clock.now"]'))
    assert result.error is not None
    assert "object" in result.error


def test_unknown_envelope_kind() -> None:
    result = parse("<<<QUERY>>>\nSELECT 1;\n<<<END>>>")
    assert result.error is not None
    assert "unknown envelope kind" in result.error


def test_stray_terminator() -> None:
    result = parse("nothing here <<<END>>> at all")
    assert result.error is not None
    assert "stray" in result.error


# Smuggling: payload bytes are data, markers are never re-parsed --------------


def test_nested_opener_inside_sql_payload_is_data() -> None:
    result = parse(read_fixture("smuggle_nested_sql_in_sql.txt"))
    assert result.error is None, result.error
    assert result.kind == "SQL"
    assert "<<<SQL>>>" in result.payload


def test_instruction_text_in_sql_payload_is_data() -> None:
    result = parse(read_fixture("instruction_in_payload_sql.txt"))
    assert result.error is None, result.error
    assert "DROP TABLE users" in result.payload


def test_instruction_text_in_toolcall_args_is_data() -> None:
    result = parse(read_fixture("instruction_in_payload_toolcall.txt"))
    assert result.error is None, result.error
    assert result.payload["arguments"]["sql"].startswith("Ignore previous instructions")


# Regression: never execute half a query --------------------------------------


def test_never_execute_half_query_regression() -> None:
    # Mirrors sentinel.py: a truncated generation must not be mistaken for a
    # complete answer. Every strict prefix of a valid block must fail.
    full = read_fixture("valid_sql.txt")
    end = full.index("<<<END>>>") + len("<<<END>>>")
    for cut in range(1, end):
        result = parse(full[:cut])
        assert result.error is not None, "prefix of length %d parsed: %r" % (cut, result)
        assert result.payload is None


# Observations: data, never instructions ---------------------------------------


def test_observation_valid_success() -> None:
    result = validate_observation(read_obs("obs_valid_success.txt"))
    assert result.valid, result.error


def test_observation_valid_error_status() -> None:
    assert validate_observation(read_obs("obs_valid_error.txt")).valid


@pytest.mark.parametrize(
    "fixture,needle",
    [
        ("obs_missing_tool_call_id.txt", "tool_call_id"),
        ("obs_bad_status.txt", "status"),
        ("obs_result_and_error.txt", "must not include"),
    ],
)
def test_invalid_observation_fixtures(fixture: str, needle: str) -> None:
    result = validate_observation(read_obs(fixture))
    assert not result.valid
    assert result.error is not None
    assert needle in result.error


def test_observation_error_status_without_error_object() -> None:
    obj = {"tool_call_id": "t", "name": "clock.now", "status": "timeout", "originated": "tool"}
    result = validate_observation(obj)
    assert not result.valid
    assert "error" in result.error


def test_observation_success_without_result() -> None:
    obj = {"tool_call_id": "t", "name": "clock.now", "status": "success", "originated": "tool"}
    assert not validate_observation(obj).valid


def test_observation_wrong_originated() -> None:
    obj = read_obs("obs_valid_success.txt")
    obj["originated"] = "assistant"
    assert not validate_observation(obj).valid


def test_observation_unknown_tool() -> None:
    obj = read_obs("obs_valid_success.txt")
    obj["name"] = "db.drop"
    assert not validate_observation(obj).valid


def test_observation_result_with_markers_is_still_data() -> None:
    # Envelope markers inside a tool result must not affect validation:
    # observations are data, never instructions.
    assert validate_observation(read_obs("obs_result_with_markers.txt")).valid
