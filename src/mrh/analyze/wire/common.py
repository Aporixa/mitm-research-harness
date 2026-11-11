"""Shared plumbing for the per-provider wire parsers.

Each provider module supplies two functions:

    is_api_url(url) -> bool         is this a model-call endpoint?
    parse_body(ctx) -> BodyResult   tool calls, model id and output tokens
                                    from one response body

`parse_records` does the bookkeeping common to all of them (request and
response counts, pairing, HTTP errors), so a provider parser only has to
understand its own body format.

The counters exist so a capture failure is visible. A session with API
responses but no recognizable body (a new streaming format, a URL the
filter does not know) is reported as `wire_unparsed` instead of looking
like a session in which the model called no tools.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from mrh.capture.redact import carries_credential
from mrh.events import ToolCall, ToolKind
from mrh.models import normalize_model

KindOf = Callable[[str], ToolKind]


def iter_sse(body: str) -> Iterator[dict[str, Any]]:
    """JSON payloads of `data:` lines. Multi-line data fields are joined."""
    buf: list[str] = []

    def flush() -> Iterator[dict[str, Any]]:
        payload = "\n".join(buf).strip()
        buf.clear()
        if not payload or payload == "[DONE]":
            return
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(ev, dict):
            yield ev

    for line in body.splitlines():
        if not line.strip():
            yield from flush()
            continue
        if line.startswith("data:"):
            buf.append(line[5:].strip())
    yield from flush()


def header(rec: dict[str, Any], name: str) -> str:
    for k, v in (rec.get("headers") or {}).items():
        if k.lower() == name:
            return str(v)
    return ""


def json_obj(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def parse_args(raw: Any) -> dict[str, Any]:
    """Tool arguments arrive as a JSON string (Anthropic deltas, OpenAI) or
    an object (Gemini). Keep unparseable input rather than dropping it."""
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except json.JSONDecodeError:
            return {"_unparseable": raw}
        return v if isinstance(v, dict) else {"_value": v}
    return {"_value": raw}


@dataclass
class BodyCtx:
    body: str
    sse: bool
    ts: float | None
    flow_id: str
    url: str
    request_body: str | None


@dataclass
class BodyResult:
    model: str | None = None
    calls: list[ToolCall] = field(default_factory=list)
    output_tokens: int = 0
    recognized: bool = False  # the body was a model response this parser understands


@dataclass
class WireParse:
    tool_calls: list[ToolCall] = field(default_factory=list)
    models: Counter[str] = field(default_factory=Counter)  # raw model id -> responses
    tool_models: Counter[str] = field(default_factory=Counter)  # raw model id -> tool calls
    output_tokens: Counter[str] = field(default_factory=Counter)  # raw model id -> tokens
    requests: int = 0
    responses: int = 0
    errors: int = 0
    unpaired: int = 0
    api_responses: int = 0  # responses on a model endpoint
    api_errors: int = 0  # ... with HTTP status >= 400
    recognized: int = 0  # ... whose body the parser understood
    unrecognized: int = 0
    api_requests: int = 0  # requests to a model endpoint
    api_requests_authed: int = 0  # ... carrying a credential header or ?key=
    ws_messages: int = 0
    wire_format: str = ""

    def primary_model(self) -> str | None:
        """The main-loop model: the one that emitted tool calls, else the one
        that produced the most output, else the most frequent."""
        for src in (self.tool_models, self.output_tokens, self.models):
            src = Counter({k: v for k, v in src.items() if v})
            if src:
                return src.most_common(1)[0][0]
        return None

    def side_models(self) -> list[str]:
        """Models seen on the wire other than the main one (titles, summaries,
        safety checks). Compared after normalization."""
        main = normalize_model(self.primary_model())
        return sorted({m for m in self.models if normalize_model(m) != main})

    def counts(self) -> dict[str, Any]:
        return {"format": self.wire_format, "requests": self.requests, "responses": self.responses,
                "errors": self.errors, "unpaired": self.unpaired,
                "api_responses": self.api_responses, "api_errors": self.api_errors,
                "recognized": self.recognized, "unrecognized": self.unrecognized,
                "api_requests": self.api_requests, "api_requests_authed": self.api_requests_authed,
                "ws_messages": self.ws_messages, "tool_calls": len(self.tool_calls)}


def _ws_segments(msgs: list[dict[str, Any]],
                 ends: Callable[[dict[str, Any]], bool] | None) -> Iterator[tuple[str | None, list[dict[str, Any]]]]:
    """Split one WebSocket connection into responses: (client message that
    started it, server events). A new segment starts after each event for
    which `ends` is true; leftovers at the end form a final segment."""
    client: str | None = None
    seg: list[dict[str, Any]] = []
    for m in msgs:
        if m.get("from_client"):
            client = m.get("body")
            continue
        ev = json_obj(m.get("body"))
        if not isinstance(ev, dict):
            continue
        ev["_ts"] = m.get("ts")
        seg.append(ev)
        if ends and ends(ev):
            yield client, seg
            seg = []
    if seg:
        yield client, seg


def parse_records(records: Iterable[dict[str, Any]], *, wire_format: str,
                  is_api_url: Callable[[str], bool],
                  parse_body: Callable[[BodyCtx], BodyResult],
                  ws_ends: Callable[[dict[str, Any]], bool] | None = None) -> WireParse:
    """`ws_ends` marks the server event that ends one response on a
    WebSocket. Each response is handed to `parse_body` as an SSE body, so a
    provider parser handles both transports the same way."""
    out = WireParse(wire_format=wire_format)
    req_body: dict[str, str | None] = {}
    ws: dict[str, list[dict[str, Any]]] = {}
    ws_url: dict[str, str] = {}

    def handle(res: BodyResult) -> None:
        if not res.recognized:
            out.unrecognized += 1
            return
        out.recognized += 1
        if res.model:
            out.models[res.model] += 1
            out.output_tokens[res.model] += res.output_tokens
        out.tool_calls.extend(res.calls)

    for rec in records:
        direction = rec.get("direction")
        fid = str(rec.get("flow_id", ""))
        if direction == "request":
            out.requests += 1
            req_body[fid] = rec.get("body")
            if is_api_url(rec.get("url") or ""):
                out.api_requests += 1
                if carries_credential(rec):
                    out.api_requests_authed += 1
            continue
        if direction == "ws_message":
            out.ws_messages += 1
            ws.setdefault(fid, []).append(rec)
            ws_url[fid] = rec.get("url") or ""
            continue
        if direction == "error":
            out.errors += 1
            continue
        if direction != "response":
            continue
        out.responses += 1
        if rec.get("pairing_warning"):
            out.unpaired += 1
        url = rec.get("url") or ""
        if not is_api_url(url):
            continue
        out.api_responses += 1
        status = rec.get("status")
        if status == 101:
            # WebSocket handshake; its messages are parsed below.
            out.api_responses -= 1
            continue
        if isinstance(status, int) and status >= 400:
            out.api_errors += 1
            continue
        ctx = BodyCtx(body=rec.get("body") or "",
                      sse="text/event-stream" in header(rec, "content-type"),
                      ts=rec.get("ts"), flow_id=fid, url=url, request_body=req_body.get(fid))
        handle(parse_body(ctx))

    for fid, msgs in ws.items():
        url = ws_url[fid]
        if not is_api_url(url):
            continue
        for i, (client, events) in enumerate(_ws_segments(msgs, ws_ends)):
            out.responses += 1
            out.api_responses += 1
            ts = events[-1].pop("_ts", None)
            for ev in events:
                ev.pop("_ts", None)
            body = "".join(f"data: {json.dumps(ev)}\n\n" for ev in events)
            handle(parse_body(BodyCtx(body=body, sse=True, ts=ts, flow_id=f"{fid}#{i}",
                                      url=url, request_body=client)))

    for c in out.tool_calls:
        if c.model:
            out.tool_models[c.model] += 1
    return out
