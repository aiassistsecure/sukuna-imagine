"""Tests for scripts/make_tooluse_corpus.py (PR-C: tool-use corpus builder).

What is verified here:

1. Every generated <<<TOOL_CALL>>> payload validates against the tool_call
   JSON schema (tests/fixtures/tooluse_schema_mirror.json — a minimal mirror
   of PR #26's schemas/tool_call.schema.json, clearly labeled as such).
2. Every <<<TOOL_RESULT>>> relay block lifts to a wire observation that
   validates against the tool_result mirror schema AND passes
   toolproto/parser.py's validate_observation, with call->result linkage
   intact (tool_call_id pairs the observation to its call; names match).
3. Every assistant message round-trips through toolproto/parser.py's parse():
   exactly-one envelope, <<<END>>> termination. Two tools (sql.execute,
   nedb.provenance) use the JSON schemas' argument names (max_rows / key),
   which predate agreement with the prototype registry (limit / row_id) —
   for those the test asserts the ONLY parse failure is that registry-name
   divergence, proving envelope extraction itself succeeded.
4. Transition gate: TOOL_CALL -> matching TOOL_RESULT pairing, <=3 calls
   per row, no repeated identical calls, rows end terminal or pending-call.
5. Determinism: same (count, temporal_count, seed, mix) -> byte-identical rows.
6. Category coverage, meta shapes, build_prompt() usage, README framing.

The parser is resolved from the repo's toolproto/parser.py when present
(after PR #26 merges); otherwise from PR #26's branch via git.
"""

import importlib.util
import json
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO_ROOT))

import make_tooluse_corpus as builder  # noqa: E402

jsonschema = pytest.importorskip("jsonschema")

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "tooluse_schema_mirror.json").read_text()
)
TOOL_CALL_SCHEMA = FIXTURE["tool_call"]
TOOL_RESULT_SCHEMA = FIXTURE["tool_result"]

_SCHEMAS = {"tool_call": TOOL_CALL_SCHEMA, "tool_result": TOOL_RESULT_SCHEMA}
_TAXONOMY = {"categories": list(builder.CATEGORIES),
             "system_message": None, "ok": True}


def load_toolproto_parser():
    """toolproto/parser.py: repo copy once PR #26 lands, else its branch."""
    p = REPO_ROOT / "toolproto" / "parser.py"
    if p.exists():
        src = p.read_text()
        origin = str(p)
    else:
        r = subprocess.run(
            ["git", "show", "imagine-tool-use-rfc:toolproto/parser.py"],
            cwd=REPO_ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip("toolproto/parser.py unavailable (PR #26 not merged "
                        "and rfc branch not present)")
        src = r.stdout
        origin = "imagine-tool-use-rfc:toolproto/parser.py"
    spec = importlib.util.spec_from_loader("toolproto_parser_rt", loader=None)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves sys.modules[cls.__module__]; register first.
    sys.modules["toolproto_parser_rt"] = mod
    exec(compile(src, origin, "exec"), mod.__dict__)  # noqa: S102 (test-only)
    return mod


PARSER = load_toolproto_parser()

RELAY_RE = re.compile(r'^<<<TOOL_RESULT name="([^"]+)" status="(ok|error)">>>$')
# Known #26 divergence: the prototype registry predates the JSON schemas'
# argument names for these two tools (registry: limit/row_id; schemas:
# max_rows/key). The corpus follows the schemas as normative.
DIVERGENT_TOOLS = ("sql.execute", "nedb.provenance")


def gen_rows(count=80, temporal_count=15, seed=11):
    mix = builder.parse_mix(None)
    return builder.generate(count, temporal_count, seed, mix, _TAXONOMY, _SCHEMAS)


def tool_calls(row):
    """(assistant_index, payload) for each TOOL_CALL envelope in a row."""
    out = []
    for i, m in enumerate(row["messages"]):
        if m["role"] == "assistant" and "<<<TOOL_CALL>>>" in m["content"]:
            payload = json.loads(
                m["content"].split("<<<TOOL_CALL>>>", 1)[1].split("<<<END>>>")[0])
            out.append((i, payload))
    return out


def tool_results(row):
    """(message_index, name, status, payload) for each TOOL_RESULT relay."""
    out = []
    for i, m in enumerate(row["messages"]):
        if m["role"] == "user" and m["content"].startswith("<<<TOOL_RESULT"):
            header, _, rest = m["content"].partition("\n")
            name, status = RELAY_RE.match(header).groups()
            payload = json.loads(rest.rsplit("\n<<<END>>>", 1)[0])
            out.append((i, name, status, payload))
    return out


def lift_to_wire(name, status, payload, tool_call_id):
    """Corpus relay block -> RFC wire observation (what the host stamps)."""
    obs = {"tool_call_id": tool_call_id, "name": name,
           "status": "success" if status == "ok" else "error",
           "originated": "tool"}
    if status == "ok":
        obs["result"] = payload
    else:
        # Corpus error payloads may carry a host hint; the wire error object
        # is exactly {code, message}.
        obs["error"] = {"code": payload["code"], "message": payload["message"]}
    return obs


# ---------------------------------------------------------------- tests ---

def test_tool_call_payloads_validate_against_schema():
    rows = gen_rows()
    n = 0
    for row in rows:
        for _, payload in tool_calls(row):
            jsonschema.validate(payload, TOOL_CALL_SCHEMA)
            n += 1
    assert n > 0, "expected at least one tool call in generated rows"
    # every v1 tool appears across a large enough sample
    names = {p["name"] for _, p in
             [c for row in gen_rows(400, 0, seed=23) for c in tool_calls(row)]}
    assert names == set(builder.V1_TOOLS), f"missing tools: {set(builder.V1_TOOLS) - names}"


def test_tool_result_lift_validates_and_links():
    rows = gen_rows()
    pairs = 0
    for row in rows:
        calls = tool_calls(row)
        results = tool_results(row)
        assert len(results) <= len(calls)
        seen_ids = set()
        for j, (ri, name, status, payload) in enumerate(results):
            ci, call_payload = calls[j]
            assert ci < ri, "result must follow its call"
            assert name == call_payload["name"], "result name must match call name"
            tcid = f"tc-{j + 1:04d}"
            assert tcid not in seen_ids
            seen_ids.add(tcid)
            obs = lift_to_wire(name, status, payload, tcid)
            jsonschema.validate(obs, TOOL_RESULT_SCHEMA)
            v = PARSER.validate_observation(obs)
            assert v.valid, f"parser rejected lifted observation: {v.error}"
            pairs += 1
    assert pairs > 0, "expected at least one call->result pair"


def test_parser_envelope_round_trip():
    rows = gen_rows()
    checked = 0
    diverged = 0
    for row in rows:
        saw_result = False
        for m in row["messages"]:
            if m["role"] == "user" and m["content"].startswith("<<<TOOL_RESULT"):
                saw_result = True
                continue
            if m["role"] != "assistant":
                continue
            res = PARSER.parse(m["content"])
            if "<<<" not in m["content"]:
                # Plain-text summary: legal only after a tool result.
                assert saw_result, "plain-text assistant turn with no prior tool result"
                assert res.error and "no envelope found" in res.error
                checked += 1
                continue
            if not res.ok and res.error.startswith("argument schema violation"):
                payload = json.loads(
                    m["content"].split("<<<TOOL_CALL>>>", 1)[1].split("<<<END>>>")[0])
                assert payload["name"] in DIVERGENT_TOOLS, res.error
                # The failure is ONLY the registry arg-name divergence: envelope
                # extraction, JSON parse, and tool lookup all succeeded.
                diverged += 1
                checked += 1
                continue
            assert res.ok, f"parser failed on: {m['content'][:120]!r}: {res.error}"
            checked += 1
    assert checked > 0
    assert diverged > 0, "expected the known registry divergence to be exercised"


def test_transition_gate():
    rows = gen_rows()
    for row in rows:
        calls = tool_calls(row)
        results = tool_results(row)
        assert len(calls) <= 3, "at most 3 tool calls per row"
        assert len(results) <= len(calls)
        # pairing: j-th result matches j-th call, in order
        for j, (ri, name, _s, _p) in enumerate(results):
            ci, call_payload = calls[j]
            assert ci < ri and call_payload["name"] == name
        # no identical call repeated
        keys = [(p["name"], json.dumps(p["arguments"], sort_keys=True))
                for _, p in calls]
        assert len(keys) == len(set(keys)), "repeated identical call"
        # row ending: terminal, or a lone pending TOOL_CALL turn
        last = row["messages"][-1]
        if len(calls) > len(results):
            assert len(calls) == 1 and len(results) == 0
            assert last["role"] == "assistant" and "<<<TOOL_CALL>>>" in last["content"]
        else:
            assert last["role"] == "assistant"


def test_determinism():
    a = gen_rows(60, 12, seed=99)
    b = gen_rows(60, 12, seed=99)
    sa = "\n".join(json.dumps(r, sort_keys=True) for r in a)
    sb = "\n".join(json.dumps(r, sort_keys=True) for r in b)
    assert sa == sb, "same seed must produce byte-identical rows"
    c = gen_rows(60, 12, seed=100)
    sc = "\n".join(json.dumps(r, sort_keys=True) for r in c)
    assert sa != sc, "different seeds should diverge"


def test_category_coverage_and_meta():
    rows = builder.generate(220, 0, seed=5, mix=builder.parse_mix(None),
                            taxonomy=_TAXONOMY, schemas=_SCHEMAS)
    cats = {r["meta"]["category"] for r in rows}
    assert cats == set(builder.CATEGORIES), f"missing: {set(builder.CATEGORIES) - cats}"
    for r in rows:
        assert r["meta"]["kind"] == "tool-use"
        assert set(r["messages"][0].keys()) == {"role", "content"}
        assert r["messages"][0]["role"] == "system"

    trows = builder.generate(0, 30, seed=5, mix=builder.parse_mix(None),
                             taxonomy=_TAXONOMY, schemas=_SCHEMAS)
    tcats = [r["meta"]["category"] for r in trows]
    assert tcats == [f"T-{i:02d}" for i in range(1, 16)] * 2
    assert all(r["meta"]["kind"] == "temporal-grounding" for r in trows)


def test_prompts_use_build_prompt():
    from stealth.forge import build_prompt as real_build_prompt
    from stealth.schemas import Schema, Table, Column
    s = Schema(key="x", domain="x",
               tables=[Table(name="t", columns=[Column("a", "integer")], rows=[])])
    rb = builder.RowBuilder(random.Random(0), "tool-use", "no-tool reasoning")
    rb.prompt(s, "hi?")
    assert rb._events[0]["content"] == real_build_prompt(s, "hi?")[1]["content"]

    # Row-level proxy: every build_prompt-rendered user turn carries the
    # canonical "Schema:\nCREATE TABLE ... \n\nQuestion: " shape.
    for row in gen_rows(120, 15, seed=31):
        for m in row["messages"]:
            if m["role"] == "user" and "\n\nQuestion: " in m["content"]:
                assert m["content"].startswith("Schema:\nCREATE TABLE"), \
                    m["content"][:80]


def test_sample_file_valid():
    p = SCRIPTS / "sample_tooluse_corpus.jsonl"
    assert p.exists(), "sample output must be checked in"
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    assert 15 <= len(rows) <= 25, f"expected ~20 sample rows, got {len(rows)}"
    kinds = {r["meta"]["kind"] for r in rows}
    assert kinds == {"tool-use", "temporal-grounding"}, kinds
    for r in rows:
        assert r["messages"][0]["role"] == "system"
        assert "category" in r["meta"]


def test_no_hardcoded_final_split():
    # The default mix is labeled a hypothesis; the README states the framing.
    assert builder.DEFAULT_MIX_HYPOTHESIS  # non-empty
    assert abs(sum(builder.DEFAULT_MIX_HYPOTHESIS.values()) - 1.0) < 1e-9
    readme = (SCRIPTS / "tooluse_corpus_README.md").read_text().lower()
    assert "cap, not a target" in readme
    assert "phase 4" in readme
