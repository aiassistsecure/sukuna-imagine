"""toolproto: deterministic protocol parser prototype for Project Imagine.

Design ancestor: stealth/sentinel.py (aiassistsecure/sukuna-imagine). See
toolproto.parser for the full design notes. Stdlib only, so a host (Hearth,
AiAssist Secure) can vendor it without extra dependencies.
"""

from .parser import (
    CLARIFY,
    RECOGNIZED_KINDS,
    SQL,
    TOOL_CALL,
    TOOL_REGISTRY,
    UNANSWERABLE,
    ParseResult,
    ToolSpec,
    ValidationResult,
    parse,
    validate_observation,
)

__all__ = [
    "CLARIFY",
    "RECOGNIZED_KINDS",
    "SQL",
    "TOOL_CALL",
    "TOOL_REGISTRY",
    "UNANSWERABLE",
    "ParseResult",
    "ToolSpec",
    "ValidationResult",
    "parse",
    "validate_observation",
]
