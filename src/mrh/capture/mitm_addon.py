"""mitmproxy addon: record model-API traffic as JSONL.

Loaded by mitmdump inside the capture container:

    mitmdump -s mitm_addon.py --set harness_out=/session/wire.jsonl \
             --set harness_hosts=api.anthropic.com

Changes from v1:
  * Credentials are redacted at capture time. v1 wrote raw `x-api-key` and
    `authorization` headers to disk and relied on the scrubber dropping
    the whole file later. v2 never writes them: each is replaced by the
    first 12 hex characters of its SHA-256, so two sessions can still be
    shown to use the same key without the key existing on disk.
  * Target hosts are configurable, so adapters for other agents can reuse
    the addon.
  * Request/response pairing and latency are as in v1.
  * WebSocket messages on a matched host are recorded one per line
    (`direction: ws_message`). Codex streams Responses API events over a
    WebSocket before falling back to HTTPS.
"""

from __future__ import annotations

import fcntl
import json
import time
from collections import OrderedDict
from typing import Any

from mitmproxy import ctx, http

from mrh.capture.redact import redact_headers, redact_url


class HarnessCapture:
    MAX_INFLIGHT = 1024

    def __init__(self) -> None:
        self._started: OrderedDict[str, float] = OrderedDict()

    def load(self, loader: Any) -> None:
        loader.add_option("harness_out", str, "", "JSONL file to append wire records to")
        loader.add_option("harness_hosts", str, "api.anthropic.com",
                          "Comma-separated hosts to record")

    def _hosts(self) -> list[str]:
        return [h.strip() for h in ctx.options.harness_hosts.split(",") if h.strip()]

    def _match(self, flow: http.HTTPFlow) -> bool:
        host = flow.request.pretty_host
        # "-" covers regional prefixes such as us-central1-aiplatform.googleapis.com.
        return any(host == h or host.endswith("." + h) or host.endswith("-" + h)
                   for h in self._hosts())

    def _append(self, rec: dict[str, Any]) -> None:
        path = ctx.options.harness_out
        if not path:
            raise RuntimeError("harness_out is not set; refusing to guess a location")
        with open(path, "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(json.dumps(rec, default=str) + "\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def request(self, flow: http.HTTPFlow) -> None:
        if not self._match(flow):
            return
        now = time.time()
        self._started[flow.id] = now
        while len(self._started) > self.MAX_INFLIGHT:
            self._started.popitem(last=False)
        self._append({
            "ts": now,
            "direction": "request",
            "flow_id": flow.id,
            "method": flow.request.method,
            "url": redact_url(flow.request.pretty_url),
            "headers": redact_headers(dict(flow.request.headers)),
            "body": flow.request.get_text(strict=False) if flow.request.content else None,
        })

    def response(self, flow: http.HTTPFlow) -> None:
        if not self._match(flow) or flow.response is None:
            return
        now = time.time()
        started = self._started.pop(flow.id, None)
        self._append({
            "ts": now,
            "direction": "response",
            "flow_id": flow.id,
            "status": flow.response.status_code,
            "url": redact_url(flow.request.pretty_url),
            "headers": redact_headers(dict(flow.response.headers)),
            "body": flow.response.get_text(strict=False) if flow.response.content else None,
            "latency_ms": None if started is None else round((now - started) * 1000, 1),
            "pairing_warning": started is None,
        })

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        if not self._match(flow) or flow.websocket is None or not flow.websocket.messages:
            return
        msg = flow.websocket.messages[-1]
        self._append({
            "ts": msg.timestamp,
            "direction": "ws_message",
            "flow_id": flow.id,
            "url": redact_url(flow.request.pretty_url),
            "from_client": msg.from_client,
            "body": msg.text if msg.is_text else None,
            "binary_len": None if msg.is_text else len(msg.content),
        })

    def error(self, flow: http.HTTPFlow) -> None:
        if self._started.pop(flow.id, None) is not None and self._match(flow):
            self._append({
                "ts": time.time(),
                "direction": "error",
                "flow_id": flow.id,
                "url": redact_url(flow.request.pretty_url),
                "error": str(flow.error) if flow.error else None,
            })


addons = [HarnessCapture()]
