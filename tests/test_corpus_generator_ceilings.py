"""Tests for the per-run ceilings of the v13 candidate generators.

scripts/make_toolproto_protocol.py and scripts/make_discrimination_corpus.py
used to die partway through large runs ("could not build row 396
(shape=chain_2)" at --count 2700; "could not build pair 290 (axis=temporal)"
at --count 9856): prompt uniqueness was enforced globally over a finite,
templated prompt space (fixed catalog), so the space drained and the
attempt loop had no way forward. Uniqueness is now per shape/axis within
a cycle, and a unit whose space is exhausted starts a fresh cycle
instead of aborting the run.

What is verified here:

1. Both generators complete in ONE run well past the old ceilings.
2. Determinism: same (count, seed) -> identical rows, including counts
   that exercise cycle restarts.
3. Small runs (nothing exhausts) keep strict uniqueness within each
   shape/axis -- the invariant the new scoping guarantees.
4. The generated rows still pass each module's own self_check.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO_ROOT))

import make_discrimination_corpus as disc  # noqa: E402
import make_toolproto_protocol as proto  # noqa: E402


def test_protocol_completes_well_past_old_ceiling():
    rows = proto.generate(1200, 13, REPO_ROOT)  # old code died at row 396
    assert len(rows) == 1200
    assert proto.self_check(rows, REPO_ROOT) > 0


def test_protocol_determinism_past_ceiling():
    a = proto.generate(600, 7, REPO_ROOT)
    b = proto.generate(600, 7, REPO_ROOT)
    assert [json.dumps(r, sort_keys=True) for r in a] == \
        [json.dumps(r, sort_keys=True) for r in b]


def test_protocol_small_run_prompts_unique_per_shape():
    rows = proto.generate(200, 13, REPO_ROOT)  # 25/shape: nothing exhausts
    by_shape = {}
    for r in rows:
        by_shape.setdefault(r["meta"]["shape"], []).append(
            proto.normalize(r["messages"][1]["content"]))
    for shape, prompts in by_shape.items():
        assert len(set(prompts)) == len(prompts), shape


def test_discrimination_completes_well_past_old_ceiling():
    rows = disc.generate(1200, 13, REPO_ROOT)  # old code died at pair 290
    assert len(rows) == 2400
    assert disc.self_check(rows, REPO_ROOT) > 0


def test_discrimination_determinism_past_ceiling():
    a = disc.generate(600, 7, REPO_ROOT)
    b = disc.generate(600, 7, REPO_ROOT)
    assert [json.dumps(r, sort_keys=True) for r in a] == \
        [json.dumps(r, sort_keys=True) for r in b]


def test_discrimination_small_run_prompts_unique_per_axis():
    rows = disc.generate(120, 13, REPO_ROOT)  # 20/axis: nothing exhausts
    by_axis = {}
    for r in rows:
        by_axis.setdefault(r["meta"]["decisive_feature"], []).append(
            disc.normalize(r["messages"][1]["content"]))
    for axis, prompts in by_axis.items():
        assert len(set(prompts)) == len(prompts), axis
