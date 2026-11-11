"""OpenAI Responses API (`/v1/responses`, also ChatGPT-auth
`/backend-api/codex/responses`) and Chat Completions (`/chat/completions`),
JSON and SSE.

Responses API tool calls are output items:
  function_call      {call_id, name, arguments: JSON string}
  custom_tool_call   {call_id, name, input: free text}   (e.g. apply_patch)
  local_shell_call   {call_id, action: {type: exec, command: [argv]}}

In SSE they arrive as `response.output_item.added` → argument deltas →
`response.output_item.done`; `response.completed` carries the full output
list and usage. An item that was added but never finished is kept and
marked truncated.

Chat Completions tool calls arrive as `choices[].delta.tool_calls[]`
fragments keyed by index.

Over a WebSocket (Codex's first choice) the same Responses events arrive
as messages; `parse_records` splits them per response and hands each to
the SSE path.

Status: written against the published formats and fixtures, not yet
checked against a live capture.
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

_CALL_ITEMS = ("function_call", "custom_tool_call", "local_shell_call")


def is_api_url(url: str) -> bool:
    path = url.split("?", 1)[0].rstrip("/")
    return path.endswith("/responses") or path.endswith("/chat/completions")


def _request_model(ctx: BodyCtx) -> str | None:
    obj = json_obj(ctx.request_body)
    if not isinstance(obj, dict):
        return None
    # Over a WebSocket the request is {"type": "response.create", ...} with
    # the model at the top level or under "response".
    return obj.get("model") or (obj.get("response") or {}).get("model")


def _item_call(item: dict[str, Any], ctx: BodyCtx, model: str | None, kind_of: KindOf,
               truncated: bool = False, args_buf: str | None = None) -> ToolCall:
    t = item.get("type")
    tid = item.get("call_id") or item.get("id") or ""
    if t == "local_shell_call":
        action = item.get("action") or {}
        name = "local_shell"
        inp: dict[str, Any] = {k: v for k, v in action.items() if k != "type"}
    elif t == "custom_tool_call":
        name = item.get("name") or ""
        inp = {"input": item.get("input") if item.get("input") is not None else (args_buf or "")}
    else:
        name = item.get("name") or ""
        raw = item.get("arguments")
        inp = parse_args(raw if raw else (args_buf or ""))
    return ToolCall(tool_id=tid, name=name, kind=kind_of(name), input=inp, source="wire",
                    ts=ctx.ts, model=model, truncated_stream=truncated)


def _usage_out(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)


def make_body_parser(kind_of: KindOf) -> Callable[[BodyCtx], BodyResult]:
    def responses_sse(ctx: BodyCtx, res: BodyResult, events: list[dict[str, Any]]) -> None:
        added: dict[str, dict[str, Any]] = {}  # item id -> item
        bufs: dict[str, str] = {}
        done: dict[str, ToolCall] = {}
        final_output: list[Any] | None = None
        for ev in events:
            et = ev.get("type") or ""
            if et in ("response.created", "response.in_progress", "response.completed",
                      "response.incomplete", "response.failed"):
                res.recognized = True
                r = ev.get("response") or {}
                res.model = r.get("model") or res.model
                if et == "response.completed":
                    res.output_tokens = _usage_out(r.get("usage"))
                    final_output = r.get("output")
            elif et == "response.output_item.added":
                item = ev.get("item") or {}
                if item.get("type") in _CALL_ITEMS:
                    added[item.get("id") or item.get("call_id") or ""] = item
            elif et in ("response.function_call_arguments.delta",
                        "response.custom_tool_call_input.delta"):
                iid = ev.get("item_id") or ""
                bufs[iid] = bufs.get(iid, "") + (ev.get("delta") or "")
            elif et == "response.output_item.done":
                item = ev.get("item") or {}
                if item.get("type") in _CALL_ITEMS:
                    iid = item.get("id") or item.get("call_id") or ""
                    added.pop(iid, None)
                    c = _item_call(item, ctx, res.model, kind_of, args_buf=bufs.get(iid))
                    done[c.tool_id] = c
        # The completed response repeats the output list; use it for any call
        # whose done event was missed.
        for item in final_output or []:
            if isinstance(item, dict) and item.get("type") in _CALL_ITEMS:
                c = _item_call(item, ctx, res.model, kind_of)
                if c.tool_id not in done:
                    done[c.tool_id] = c
                added.pop(item.get("id") or "", None)
        for iid, item in added.items():
            c = _item_call(item, ctx, res.model, kind_of, truncated=True, args_buf=bufs.get(iid))
            done.setdefault(c.tool_id, c)
        for c in done.values():
            c.model = c.model or res.model
        res.calls.extend(done.values())

    def chat_sse(ctx: BodyCtx, res: BodyResult, events: list[dict[str, Any]]) -> None:
        parts: dict[int, dict[str, Any]] = {}
        finished = False
        for ev in events:
            if ev.get("object") != "chat.completion.chunk":
                continue
            res.recognized = True
            res.model = ev.get("model") or res.model
            if ev.get("usage"):
                res.output_tokens = _usage_out(ev["usage"])
            for ch in ev.get("choices") or []:
                if ch.get("finish_reason"):
                    finished = True
                for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                    p = parts.setdefault(int(tc.get("index", 0)), {"id": "", "name": "", "args": ""})
                    p["id"] = tc.get("id") or p["id"]
                    fn = tc.get("function") or {}
                    p["name"] = fn.get("name") or p["name"]
                    p["args"] += fn.get("arguments") or ""
        for p in parts.values():
            res.calls.append(ToolCall(tool_id=p["id"], name=p["name"], kind=kind_of(p["name"]),
                                      input=parse_args(p["args"]), source="wire", ts=ctx.ts,
                                      model=res.model, truncated_stream=not finished))

    def parse_body(ctx: BodyCtx) -> BodyResult:
        res = BodyResult(model=_request_model(ctx))
        if ctx.sse:
            events = list(iter_sse(ctx.body))
            if any(str(e.get("type", "")).startswith("response.") for e in events):
                responses_sse(ctx, res, events)
            else:
                chat_sse(ctx, res, events)
            return res

        obj = json_obj(ctx.body)
        if not isinstance(obj, dict):
            return res
        if obj.get("object") == "response":
            res.recognized = True
            res.model = obj.get("model") or res.model
            res.output_tokens = _usage_out(obj.get("usage"))
            for item in obj.get("output") or []:
                if isinstance(item, dict) and item.get("type") in _CALL_ITEMS:
                    res.calls.append(_item_call(item, ctx, res.model, kind_of))
        elif obj.get("object") == "chat.completion":
            res.recognized = True
            res.model = obj.get("model") or res.model
            res.output_tokens = _usage_out(obj.get("usage"))
            for ch in obj.get("choices") or []:
                for tc in (ch.get("message") or {}).get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    res.calls.append(ToolCall(
                        tool_id=tc.get("id") or "", name=fn.get("name") or "",
                        kind=kind_of(fn.get("name") or ""), input=parse_args(fn.get("arguments")),
                        source="wire", ts=ctx.ts, model=res.model))
        return res

    return parse_body


_WS_END = {"response.completed", "response.failed", "response.incomplete", "error"}


def parse_wire(records: Iterable[dict[str, Any]], kind_of: KindOf) -> WireParse:
    return parse_records(records, wire_format="openai", is_api_url=is_api_url,
                         parse_body=make_body_parser(kind_of),
                         ws_ends=lambda ev: ev.get("type") in _WS_END)
