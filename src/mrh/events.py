"""Normalized records shared by every adapter and analyzer.

Adapters translate agent-specific formats (Claude Code stream-json, the
Anthropic Messages wire format, ...) into these types. Analyzers only ever
see these types, which is what makes the analysis agent-agnostic.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

ToolKind = Literal["shell", "file_read", "file_write", "file_edit", "fetch", "search", "mcp", "other"]


@dataclass
class ToolCall:
    """A tool invocation the model emitted."""

    tool_id: str
    name: str
    kind: ToolKind
    input: dict[str, Any]
    source: Literal["wire", "stream"]
    ts: float | None = None  # epoch seconds
    model: str | None = None  # model id from the request that produced it (wire only)
    truncated_stream: bool = False


@dataclass
class ToolResult:
    """What the agent runtime fed back to the model for a tool call."""

    tool_id: str
    is_error: bool
    content: str
    ts: float | None = None
    # Assistant text emitted after this result and before the next tool call
    # or the final result. For refused calls this is the model's account to
    # the user of what happened.
    following_text: str | None = None


@dataclass
class Exec:
    """A process execution observed by strace."""

    pid: int
    ppid: int | None
    ts: float  # epoch seconds
    binary: str
    argv: list[str]
    ret: int | None
    truncated: bool = False


@dataclass
class FileOp:
    pid: int
    ts: float
    path: str
    mode: Literal["read", "write"]
    flags: list[str]
    ret: int | None


@dataclass
class NetConnect:
    pid: int
    ts: float
    family: str
    address: str | None
    port: int | None
    ret: int | None


@dataclass
class FinalReport:
    """The agent's final output to its consumer."""

    text: str | None
    is_error: bool = False
    num_turns: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def to_jsonl(rows: Iterable[Any], path: Path) -> int:
    """Write dataclass rows as JSONL atomically. Returns the row count."""
    from mrh.util import atomic_write

    n = 0
    with atomic_write(path) as f:
        for r in rows:
            f.write(json.dumps(asdict(r) if hasattr(r, "__dataclass_fields__") else r) + "\n")
            n += 1
    return n


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
