"""Anthropic Messages API (`/v1/messages`), JSON and SSE.

Each tool call carries the model id that produced it, taken from the
response (`message_start` for SSE, `model` for JSON), falling back to the
request body. v1 never recorded the model, which is why the 2026-05
finding could not say which model fabricated.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from mrh.analyze.wire.common import (
    BodyCtx,
    BodyResult,
    KindOf,
    WireParse,
    iter_sse,
    json_obj,
    parse_args,
    parse_records,
)
from mrh.events import ToolCall

_TOOL_BLOCKS = ("tool_use", "server_tool_use")


def is_api_url(url: str) -> bool:
    path = url.split("?", 1)[0]
    return path.endswith("/messages") and "count_tokens" not in path


def _request_model(ctx: BodyCtx) -> str | None:
    obj = json_obj(ctx.request_body)
    return obj.get("model") if isinstance(obj, dict) else None


def make_body_parser(kind_of: KindOf) -> Callable[[BodyCtx], BodyResult]:
    def parse_body(ctx: BodyCtx) -> BodyResult:
        res = BodyResult(model=_request_model(ctx))
        if ctx.sse:
            open_blocks: dict[int, dict[str, Any]] = {}
            for ev in iter_sse(ctx.body):
                et = ev.get("type")
                if et == "message_start":
                    res.recognized = True
                    msg = ev.get("message") or {}
                    res.model = msg.get("model") or res.model
                elif et == "content_block_start":
                    blk = ev.get("content_block") or {}
                    if blk.get("type") in _TOOL_BLOCKS:
                        open_blocks[ev.get("index", -1)] = {
                            "id": blk.get("id"), "name": blk.get("name"), "buf": ""}
                elif et == "content_block_delta":
                    d = ev.get("delta") or {}
                    b = open_blocks.get(ev.get("index", -1))
                    if b is not None and d.get("type") == "input_json_delta":
                        b["buf"] += d.get("partial_json", "")
                elif et == "content_block_stop":
                    b = open_blocks.pop(ev.get("index", -1), None)
                    if b is not None:
                        res.calls.append(_call(b, ctx, res.model, kind_of, truncated=False))
                elif et == "message_delta":
                    res.output_tokens = int((ev.get("usage") or {}).get("output_tokens") or 0)
            # A stream that ends with blocks still open was cut off (network
            # drop, killed session). Keep the call but mark it, so a missing
            # exec is not mistaken for a model behavior.
            for b in open_blocks.values():
                res.calls.append(_call(b, ctx, res.model, kind_of, truncated=True))
            # Calls created before message_start set the model get it now.
            for c in res.calls:
                c.model = c.model or res.model
            return res

        msg = json_obj(ctx.body)
        if not isinstance(msg, dict) or msg.get("type") != "message":
            return res
        res.recognized = True
        res.model = msg.get("model") or res.model
        res.output_tokens = int((msg.get("usage") or {}).get("output_tokens") or 0)
        for blk in msg.get("content") or []:
            if isinstance(blk, dict) and blk.get("type") in _TOOL_BLOCKS:
                res.calls.append(ToolCall(
                    tool_id=blk.get("id") or "", name=blk.get("name") or "",
                    kind=kind_of(blk.get("name") or ""), input=parse_args(blk.get("input")),
                    source="wire", ts=ctx.ts, model=res.model))
        return res

    return parse_body


def _call(b: dict[str, Any], ctx: BodyCtx, model: str | None, kind_of: KindOf,
          truncated: bool) -> ToolCall:
    return ToolCall(tool_id=b.get("id") or "", name=b.get("name") or "",
                    kind=kind_of(b.get("name") or ""), input=parse_args(b["buf"]),
                    source="wire", ts=ctx.ts, model=model, truncated_stream=truncated)


def parse_wire(records: Iterable[dict[str, Any]], kind_of: KindOf) -> WireParse:
    return parse_records(records, wire_format="anthropic-messages", is_api_url=is_api_url,
                         parse_body=make_body_parser(kind_of))
