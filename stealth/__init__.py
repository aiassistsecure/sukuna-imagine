"""stealth — execution-gated text-to-SQL corpus forge and training harness.

Part of project `imagine` (Interchained). The goal: a ~1B model that runs on
an ordinary CPU and writes correct PostgreSQL from natural language.

The load-bearing idea is that text-to-SQL has a PERFECT verifier — PostgreSQL
itself — so corpus quality can be machine-guaranteed rather than assumed.
"""
__version__ = "0.1.0"

from .sentinel import wrap, extract, extract_one, sql_of, Block  # noqa: F401
from .gate import Gate, GateResult, Level, digest_rows           # noqa: F401
