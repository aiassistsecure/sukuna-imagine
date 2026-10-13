"""Deterministic protocol parser prototype for Project Imagine tool use.

Design ancestor: stealth/sentinel.py (aiassistsecure/sukuna-imagine): blocks
are extracted by delimiter and NEVER re-parsed. This package mirrors those
principles so a host (Hearth, AiAssist Secure) can vendor it standalone; it
does NOT import from stealth/.

Mirrored principles:
- Envelopes are literal delimited blocks: an opener like <<<SQL>>> plus the
  shared terminator <<<END>>>. For SQL, CLARIFY, UNANSWERABLE, extraction is
  a non-greedy DOTALL match on the literal markers, exactly like sentinel.py.
  Payload bytes are data: never re-parsed, never interpreted.
- Exactly-one invariant: one assistant turn holds exactly one envelope.
  sentinel.py's extract_one returns None for zero or multiple blocks; here
  that is an explicit error, never a guess.
- Unterminated blocks yield nothing. sentinel.py: "silently accepting half
  a query is how you execute half a query." Truncation is an error, never a
  partial answer.

Tool-use extensions (new here):
- TOOL_CALL payloads are JSON, extracted with a brace-aware scan (strings and
  escapes respected) so a marker inside a JSON string never ends the block.
  Any envelope marker inside a tool-call payload is rejected as smuggling:
  malformed, never split.
- The payload must be an object with "name" in the V1 registry and an
  "arguments" object matching that tool's schema. Unknown tools, bad JSON,
  and bad arguments are all rejected.
- Observations are data, never instructions: validate_observation checks
  structure only and never scans result bytes for markers.

Payloads are kept fully verbatim (sentinel.py strips outer newlines; this
prototype does not, so tests assert byte-exact extraction).

V1 registry, all read-only: schema.inspect, clock.now, sql.explain,
sql.execute, nedb.provenance. Stdlib only: json, re, dataclasses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

SQL = "SQL"
CLARIFY = "CLARIFY"
UNANSWERABLE = "UNANSWERABLE"
TOOL_CALL = "TOOL_CALL"

RECOGNIZED_KINDS = (SQL, CLARIFY, UNANSWERABLE, TOOL_CALL)

_TOKEN_RE = re.compile(r"<<<([A-Z][A-Z0-9_]*)>>>")
_TERMINATOR_MARKER = "<<<END>>>"
_WHITESPACE = " \t\r\n"
_OBSERVATION_STATUSES = ("success", "error", "denied", "timeout")


@dataclass
class ParseResult:
    """Outcome of parsing one assistant turn: exactly one block or an error.

    On success kind is one of SQL, CLARIFY, UNANSWERABLE, TOOL_CALL; payload
    is verbatim text (str) for the text envelopes or the decoded JSON object
    (dict) for TOOL_CALL; raw is the full block including markers. On failure
    error describes why and the other fields are None.
    """

    kind: Optional[str] = None
    payload: Optional[Any] = None
    raw: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True when parsing produced a block (no error)."""
        return self.error is None


@dataclass
class ValidationResult:
    """Outcome of validating a host-side tool observation."""

    valid: bool
    error: Optional[str] = None


@dataclass(frozen=True)
class ToolSpec:
    """Argument schema for one V1 registry tool (all tools are read-only)."""

    name: str
    required: Dict[str, str]
    optional: Dict[str, str]
    description: str


TOOL_REGISTRY: Dict[str, ToolSpec] = {
    "schema.inspect": ToolSpec("schema.inspect", {}, {"scope": "string", "table": "string"},
                               "Read-only: list tables in a scope, or describe one table."),
    "clock.now": ToolSpec("clock.now", {}, {"timezone": "string"},
                          "Read-only: current time, optionally in a named timezone."),
    "sql.explain": ToolSpec("sql.explain", {"sql": "string"}, {},
                            "Read-only: explain the plan for a SQL string."),
    "sql.execute": ToolSpec("sql.execute", {"sql": "string"}, {"limit": "integer"},
                            "Read-only: run a SELECT-style query, return rows."),
    "nedb.provenance": ToolSpec("nedb.provenance", {"table": "string"}, {"row_id": "string|integer"},
                                "Read-only: provenance metadata for a table or row."),
}


def _check_type(value: Any, type_name: str) -> bool:
    """True when value matches one of the '|' separated type names."""
    for part in type_name.split("|"):
        part = part.strip()
        if part == "string" and isinstance(value, str):
            return True
        if part == "boolean" and isinstance(value, bool):
            return True
        if part == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if part == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if part == "object" and isinstance(value, dict):
            return True
        if part == "array" and isinstance(value, list):
            return True
    return False


_TYPE_LABELS = {bool: "boolean", int: "integer", float: "number", str: "string",
               list: "array", dict: "object"}


def _type_label(value: Any) -> str:
    """Short type name for error messages (exact type(), so bool != int)."""
    if value is None:
        return "null"
    return _TYPE_LABELS.get(type(value), type(value).__name__)


@dataclass
class _Block:
    kind: str
    payload_text: str
    raw: str


def _extract_balanced_json(text: str, start: int) -> Optional[Tuple[str, int]]:
    """Extract one JSON value at/after start; None when none is complete.

    Brace-aware scan respecting strings and escapes, so a marker inside a
    string never ends the value early.
    """
    i = start
    while i < len(text) and text[i] in _WHITESPACE:
        i += 1
    if i >= len(text) or text[i] not in "{[":
        return None
    depth = 0
    in_string = False
    escaped = False
    for j in range(i, len(text)):
        ch = text[j]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[i : j + 1], j + 1
    return None


def _extract_block(text: str, opener: re.Match[str]) -> Tuple[Optional[_Block], int, Optional[str]]:
    """Extract the block opened at opener. Returns (block, next_pos, error)."""
    kind = opener.group(1)
    start = opener.start()
    if kind == TOOL_CALL:
        found = _extract_balanced_json(text, opener.end())
        if found is None:
            return None, start, "malformed TOOL_CALL payload: no complete JSON value after <<<TOOL_CALL>>>"
        json_text, after = found
        i = after
        while i < len(text) and text[i] in _WHITESPACE:
            i += 1
        if not text.startswith(_TERMINATOR_MARKER, i):
            return None, start, "unterminated <<<TOOL_CALL>>> envelope: JSON payload is not followed by <<<END>>>"
        end = i + len(_TERMINATOR_MARKER)
        smuggled = _TOKEN_RE.search(json_text)
        if smuggled is not None:
            return None, start, (
                "malformed TOOL_CALL payload: smuggled envelope marker '%s' inside payload; "
                "markers are never valid tool-call data" % smuggled.group(0)
            )
        return _Block(TOOL_CALL, json_text, text[start:end]), end, None
    end_idx = text.find(_TERMINATOR_MARKER, opener.end())
    if end_idx == -1:
        return None, start, (
            "unterminated <<<%s>>> envelope: missing <<<END>>>; truncated output yields nothing, "
            "never execute half a query" % kind
        )
    end = end_idx + len(_TERMINATOR_MARKER)
    return _Block(kind, text[opener.end() : end_idx], text[start:end]), end, None


def _scan_blocks(text: str) -> Tuple[List[_Block], Optional[str]]:
    """Split text into envelope blocks, left to right. Returns (blocks, error)."""
    blocks: List[_Block] = []
    pos = 0
    while True:
        opener = _TOKEN_RE.search(text, pos)
        if opener is None:
            return blocks, None
        if opener.group(1) == "END":
            return blocks, "stray <<<END>>> terminator with no opening envelope"
        block, next_pos, error = _extract_block(text, opener)
        if error is not None:
            return blocks, error
        blocks.append(block)
        pos = next_pos


def parse(text: str) -> ParseResult:
    """Parse one assistant turn. Returns exactly one block or an error."""
    if not isinstance(text, str):
        return ParseResult(error="parse input must be a string")
    blocks, scan_error = _scan_blocks(text)
    if scan_error is not None:
        return ParseResult(error=scan_error)
    if not blocks:
        return ParseResult(error=(
            "no envelope found in turn: expected exactly one of "
            "<<<SQL>>>, <<<CLARIFY>>>, <<<UNANSWERABLE>>>, <<<TOOL_CALL>>>"))
    if len(blocks) > 1:
        kinds = ", ".join("<<<%s>>>" % b.kind for b in blocks)
        return ParseResult(error="multiple envelopes in turn (%s): expected exactly one" % kinds)
    block = blocks[0]
    if block.kind not in RECOGNIZED_KINDS:
        return ParseResult(error=(
            "unknown envelope kind <<<%s>>>: expected one of <<<SQL>>>, <<<CLARIFY>>>, "
            "<<<UNANSWERABLE>>>, <<<TOOL_CALL>>>" % block.kind))
    if block.kind == TOOL_CALL:
        return _parse_tool_call(block)
    return ParseResult(kind=block.kind, payload=block.payload_text, raw=block.raw)


def _parse_tool_call(block: _Block) -> ParseResult:
    """Validate a TOOL_CALL block's JSON payload against the registry."""
    try:
        data = json.loads(block.payload_text)
    except json.JSONDecodeError as exc:
        return ParseResult(error="TOOL_CALL payload is not valid JSON: %s" % exc)
    if not isinstance(data, dict):
        return ParseResult(error="TOOL_CALL payload must be a JSON object")
    unknown = sorted(str(k) for k in data if k not in ("name", "arguments"))
    if unknown:
        return ParseResult(error="TOOL_CALL payload has unknown fields: %s" % ", ".join(unknown))
    if "name" not in data:
        return ParseResult(error="TOOL_CALL payload missing required field 'name'")
    name = data["name"]
    if not isinstance(name, str):
        return ParseResult(error="TOOL_CALL 'name' must be a string")
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        return ParseResult(error="unknown tool '%s': expected one of %s" % (name, ", ".join(sorted(TOOL_REGISTRY))))
    if "arguments" not in data:
        return ParseResult(error="TOOL_CALL payload missing required field 'arguments'")
    arguments = data["arguments"]
    if not isinstance(arguments, dict):
        return ParseResult(error="TOOL_CALL 'arguments' must be a JSON object")
    arg_error = _validate_arguments(spec, arguments)
    if arg_error is not None:
        return ParseResult(error=arg_error)
    return ParseResult(kind=TOOL_CALL, payload=data, raw=block.raw)


def _validate_arguments(spec: ToolSpec, arguments: Dict[str, Any]) -> Optional[str]:
    """Check arguments against a tool spec. Returns an error string or None."""
    for arg_name in spec.required:
        if arg_name not in arguments:
            return "argument schema violation for tool '%s': missing required argument '%s'" % (spec.name, arg_name)
    for arg_name, value in arguments.items():
        expected = spec.required.get(arg_name, spec.optional.get(arg_name))
        if expected is None:
            return "argument schema violation for tool '%s': unknown argument '%s'" % (spec.name, arg_name)
        if not _check_type(value, expected):
            return "argument schema violation for tool '%s': argument '%s' must be %s, got %s" % (
                spec.name, arg_name, expected, _type_label(value))
    return None


def validate_observation(obj: Any) -> ValidationResult:
    """Validate a host-side tool observation. Structure only: result bytes are
    never scanned for markers, since observations are data, not instructions.
    """
    if not isinstance(obj, dict):
        return ValidationResult(False, "observation must be a JSON object")
    call_id = obj.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        return ValidationResult(False, "observation missing non-empty string 'tool_call_id'")
    name = obj.get("name")
    if not isinstance(name, str) or name not in TOOL_REGISTRY:
        return ValidationResult(False, "observation has unknown tool name %r: expected one of %s" % (
            name, ", ".join(sorted(TOOL_REGISTRY))))
    status = obj.get("status")
    if status not in _OBSERVATION_STATUSES:
        return ValidationResult(False, "observation has invalid status %r: expected one of success, error, denied, timeout" % (status,))
    if obj.get("originated") != "tool":
        return ValidationResult(False, "observation 'originated' must be the string 'tool'")
    has_result = "result" in obj
    has_error = "error" in obj
    if status == "success":
        if not has_result:
            return ValidationResult(False, "observation with status 'success' must include 'result'")
        if has_error:
            return ValidationResult(False, "observation with status 'success' must not include 'error'")
    else:
        if has_result:
            return ValidationResult(False, "observation with status '%s' must not include 'result'" % status)
        if not has_error:
            return ValidationResult(False, "observation with status '%s' must include 'error'" % status)
        if not isinstance(obj["error"], dict):
            return ValidationResult(False, "observation 'error' must be an object")
    return ValidationResult(True, None)
