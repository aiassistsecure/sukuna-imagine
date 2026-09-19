"""stealth :: sentinel-block SQL envelope

Dogfoods Interchained's sentinel-blocks format for the model's output. SQL is
the payload that motivates the technique: it is wall-to-wall single quotes,
double quotes, semicolons, newlines and dollar-quoting. Any format that
re-parses the payload -- JSON string escaping, markdown fences, regex over
`SELECT.*` -- will eventually corrupt a legitimate query.

Sentinels are extracted by delimiter and NEVER re-parsed, so the bytes between
the markers are exactly the bytes the model emitted.

    <<<SQL>>>
    SELECT count(*) FROM orders WHERE status = 'paid';
    <<<END>>>

Two more blocks exist so the model can decline or ask instead of guessing --
which is the text-to-SQL equivalent of the "don't call a tool" case that the
imagine harness proved matters:

    <<<UNANSWERABLE>>>   the schema cannot answer this question
    <<<CLARIFY>>>        the question is ambiguous; state what is missing
"""
from __future__ import annotations

import re
from dataclasses import dataclass

OPEN_SQL = "<<<SQL>>>"
OPEN_UNANSWERABLE = "<<<UNANSWERABLE>>>"
OPEN_CLARIFY = "<<<CLARIFY>>>"
CLOSE = "<<<END>>>"

# Non-greedy, DOTALL, anchored on the literal markers. Deliberately NOT a
# parser: it finds delimiters and slices. The payload is never interpreted.
_BLOCK = re.compile(
    r"<<<(SQL|UNANSWERABLE|CLARIFY)>>>(.*?)<<<END>>>",
    re.DOTALL,
)


@dataclass(frozen=True)
class Block:
    kind: str          # "SQL" | "UNANSWERABLE" | "CLARIFY"
    payload: str       # verbatim bytes between the markers, stripped of outer newlines
    raw: str           # the full block including markers


def wrap(sql: str, kind: str = "SQL") -> str:
    """Produce a sentinel block. Used to build training targets."""
    if kind not in ("SQL", "UNANSWERABLE", "CLARIFY"):
        raise ValueError(f"unknown block kind: {kind!r}")
    body = sql.strip("\n")
    return f"<<<{kind}>>>\n{body}\n{CLOSE}"


def extract(text: str) -> list[Block]:
    """Return every sentinel block in `text`, in order.

    An unterminated block yields nothing -- a truncated generation must not
    be mistaken for a complete answer. That is a deliberate choice: silently
    accepting half a query is how you execute half a query.
    """
    out: list[Block] = []
    for m in _BLOCK.finditer(text):
        out.append(Block(kind=m.group(1),
                         payload=m.group(2).strip("\n"),
                         raw=m.group(0)))
    return out


def extract_one(text: str) -> Block | None:
    """The single expected block, or None.

    Returns None when there are zero blocks OR more than one: emitting two
    answers is not the same as emitting one, and picking the first would
    paper over a model that could not decide.
    """
    blocks = extract(text)
    return blocks[0] if len(blocks) == 1 else None


def sql_of(text: str) -> str | None:
    """Convenience: the SQL payload if the reply is exactly one SQL block."""
    b = extract_one(text)
    return b.payload if b and b.kind == "SQL" else None
