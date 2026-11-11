"""Analyze one captured session directory into analysis.json."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mrh import SCHEMA_VERSION
from mrh.adapters import get_adapter
from mrh.analyze.correlate import correlate
from mrh.analyze.refusal import classify, outcome
from mrh.capture.strace import parse_strace
from mrh.events import read_jsonl, to_jsonl
from mrh.models import normalize_model
from mrh.util import write_json_atomic


def analyze_session(session: Path, expected_output: str | None = None,
                    expected_pattern: str | None = None) -> dict[str, Any]:
    manifest_path = session / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    adapter = get_adapter(manifest.get("adapter", "claude-code"))
    trial = manifest.get("trial") or {}
    if expected_output is None:
        expected_output = trial.get("expected_output")
    if expected_pattern is None:
        expected_pattern = trial.get("expected_pattern")

    wire_path = session / "wire.jsonl"
    wire = adapter.parse_wire(read_jsonl(wire_path) if wire_path.exists() else [])

    stream_path = session / "stream.jsonl"
    with (stream_path.open() if stream_path.exists() else open("/dev/null")) as f:
        stream = adapter.parse_stream(f)

    strace_path = session / "strace.log"
    trace = None
    if strace_path.exists():
        with strace_path.open(errors="replace") as f:
            trace = parse_strace(f)

    corr = correlate(adapter, wire, stream, trace)
    corr_d = corr.to_dict()
    final_text = stream.final.text if stream.final else None
    refusal = classify(corr_d["calls"], final_text, expected_output, expected_pattern)
    result = outcome(corr_d, final_text, expected_output, expected_pattern)
    primary = wire.primary_model()
    requested = trial.get("model")

    # A compact, scrub-safe record of what the model emitted. The raw wire
    # file holds the full system prompt and conversation and is dropped by
    # the scrubber; this one survives it.
    to_jsonl(wire.tool_calls, session / "wire.calls.jsonl")

    analysis = {
        "schema": SCHEMA_VERSION,
        "session": session.name,
        # Only the wire counts as observed. The agent's own init event says what it
        # *intends* to use; keeping it separate stops a capture failure being read as
        # "ran on the requested model".
        "model": primary,
        "model_normalized": normalize_model(primary),
        "model_source": "wire" if primary else None,
        "model_requested": requested,
        # Requested vs observed, compared on normalized ids so a dated
        # snapshot or a [1m] tag is not reported as a different model.
        "model_matches_request": None if not (requested and primary)
        else normalize_model(requested) == normalize_model(primary),
        # Side models (titles, summaries, safety checks) are not the model
        # under test; keep them out of `model`.
        "models_side": wire.side_models(),
        "models_seen": dict(wire.models),
        "output_tokens": dict(wire.output_tokens),
        "agent_reported": stream.init,
        "wire": wire.counts(),
        "trace": trace.summary() if trace else None,
        "final": None if stream.final is None else {
            "text": final_text, "is_error": stream.final.is_error,
            "num_turns": stream.final.num_turns, **stream.final.extra},
        "correlation": corr_d,
        "refusal": refusal,
        "outcome": result,
        "expected_output": expected_output,
        "expected_pattern": expected_pattern,
        "verdict_counts": _count(c["verdict"] for c in corr_d["calls"]),
    }
    sandbox_failed = any(SANDBOX_FAILURE.search(r.content or "") for r in stream.tool_results)
    problems = capture_problems(manifest, wire, trace is not None, stream.final is not None,
                                corr.used_stream_fallback, sandbox_failed)
    analysis["capture_problems"] = problems
    analysis["capture_ok"] = not problems
    write_json_atomic(session / "analysis.json", analysis)
    return analysis


# The agent's own sandbox could not start (in a container, usually because
# unprivileged user namespaces are blocked). Commands then fail for a reason
# that has nothing to do with the model, and can look like refusals.
SANDBOX_FAILURE = re.compile(
    r"bwrap: |No permissions to create a new namespace|sandbox-exec: |"
    r"landlock[^\n]{0,40}(?:not supported|unavailable|failed)", re.I)


def capture_problems(manifest: dict[str, Any], wire: Any, have_trace: bool, have_final: bool,
                     used_stream_fallback: bool, sandbox_failed: bool = False) -> list[str]:
    """Reasons this session cannot support a claim. Empty means usable.

    Each is a capture failure, not a model behavior, and must not be counted
    as one (e.g. "no tool calls on the wire" because the proxy was bypassed).
    """
    p: list[str] = []
    status = manifest.get("status")
    if status and status != "exited":
        p.append(f"status_{status}")
    if not wire.responses:
        p.append("no_wire")
    elif not wire.api_responses:
        p.append("no_api_responses")  # traffic seen, none on a model endpoint
    elif wire.unrecognized and not wire.recognized:
        p.append("wire_unparsed")  # model endpoint seen, body format unknown
    if wire.api_errors and not wire.recognized:
        p.append("api_errors_only")  # auth/quota/model errors; agent never ran a turn
    if getattr(wire, "api_requests", 0) and not getattr(wire, "api_requests_authed", 0):
        p.append("credential_not_sent")  # agent did not pick up the credential it was given
    if sandbox_failed:
        p.append("agent_sandbox_failed")
    if used_stream_fallback:
        p.append("stream_fallback")
    if not have_trace:
        p.append("no_trace")
    if not have_final:
        p.append("no_final_report")
    return p


def _count(it: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out
