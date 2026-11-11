"""Wire parsers, one per model-API format. Adapters pick one by name."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from mrh.analyze.wire import anthropic, gemini, openai
from mrh.analyze.wire.common import KindOf, WireParse

WireParser = Callable[[Iterable[dict[str, Any]], KindOf], WireParse]

PARSERS: dict[str, WireParser] = {
    "anthropic-messages": anthropic.parse_wire,
    "openai": openai.parse_wire,
    "gemini": gemini.parse_wire,
}

__all__ = ["PARSERS", "WireParse", "WireParser"]
