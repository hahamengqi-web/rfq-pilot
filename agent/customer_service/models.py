"""Small, explicit contracts shared by customer Harness components."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RouteDecision:
    intent: str
    confidence: float
    product_model: str = ""
    needs_retrieval: bool = False
    needs_clarification: bool = False
    missing_fields: tuple[str, ...] = ()
    handoff_required: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ConversationContext:
    summary: str = ""
    recent_turns: tuple[dict[str, str], ...] = ()
    state: dict[str, Any] = field(default_factory=dict)
    long_term_memories: tuple[str, ...] = ()
