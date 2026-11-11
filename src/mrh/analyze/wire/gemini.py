"""Google Gemini `generateContent` / `streamGenerateContent`, as used by
the Gemini API (generativelanguage.googleapis.com), Vertex AI
(*-aiplatform.googleapis.com) and Code Assist (cloudcode-pa.googleapis.com,
used by Gemini CLI with Google login).

Responses are GenerateContentResponse objects, sent as SSE (`alt=sse`), a
JSON array (streaming without alt=sse) or a single JSON object. Code
Assist wraps each one as `{"response": {...}}`. Tool calls are
`functionCall` parts; their `args` arrive whole, not as deltas, so a
stream that was cut off loses the call rather than truncating it.

`functionCall.id` is optional. When it is missing the id is synthesized
from the flow id and position, and the correlator aligns stream and wire
calls by order and command instead of by id.

Status: written against the published formats and fixtures, not yet
checked against a live capture.
"""

from __future__ import annotations

import re
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

_URL_MODEL = re.compile(r"/models/([^/:?]+)")


def is_api_url(url: str) -> bool:
    path = url.split("?", 1)[0]
    return path.endswith(":generateContent") or path.endswith(":streamGenerateContent")


def _request_model(ctx: BodyCtx) -> str | None:
    m = _URL_MODEL.search(ctx.url)
    if m:
        return m.group(1)
    obj = json_obj(ctx.request_body)
    return obj.get("model") if isinstance(obj, dict) else None


def _chunks(ctx: BodyCtx) -> list[dict[str, Any]]:
    if ctx.sse:
        raw: list[Any] = list(iter_sse(ctx.body))
    else:
        obj = json_obj(ctx.body)
        raw = obj if isinstance(obj, list) else [obj] if isinstance(obj, dict) else []
    out = []
    for c in raw:
        if isinstance(c, dict) and isinstance(c.get("response"), dict):
            c = c["response"]  # Code Assist envelope
        if isinstance(c, dict) and ("candidates" in c or "usageMetadata" in c):
            out.append(c)
    return out


def make_body_parser(kind_of: KindOf) -> Callable[[BodyCtx], BodyResult]:
    def parse_body(ctx: BodyCtx) -> BodyResult:
        res = BodyResult(model=_request_model(ctx))
        n = 0
        for chunk in _chunks(ctx):
            res.recognized = True
            res.model = chunk.get("modelVersion") or res.model
            usage = chunk.get("usageMetadata") or {}
            if usage.get("candidatesTokenCount"):
                res.output_tokens = int(usage["candidatesTokenCount"])
            for cand in chunk.get("candidates") or []:
                for part in (cand.get("content") or {}).get("parts") or []:
                    fc = part.get("functionCall") if isinstance(part, dict) else None
                    if not isinstance(fc, dict):
                        continue
                    name = fc.get("name") or ""
                    res.calls.append(ToolCall(
                        tool_id=fc.get("id") or f"{ctx.flow_id}#{n}", name=name, kind=kind_of(name),
                        input=parse_args(fc.get("args")), source="wire", ts=ctx.ts))
                    n += 1
        for c in res.calls:
            c.model = res.model
        return res

    return parse_body


def parse_wire(records: Iterable[dict[str, Any]], kind_of: KindOf) -> WireParse:
    return parse_records(records, wire_format="gemini", is_api_url=is_api_url,
                         parse_body=make_body_parser(kind_of))
