"""Adapter for Claude Code (`claude --print --output-format stream-json`)."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any, ClassVar

from mrh.adapters.base import Adapter, LaunchSpec, StreamParse, content_text, read_json_lines
from mrh.events import FinalReport, ToolCall, ToolKind, ToolResult

_KINDS: dict[str, ToolKind] = {
    "Bash": "shell",
    "BashOutput": "other",
    "KillShell": "other",
    "Read": "file_read",
    "Write": "file_write",
    "Edit": "file_edit",
    "MultiEdit": "file_edit",
    "NotebookEdit": "file_edit",
    "WebFetch": "fetch",
    "WebSearch": "fetch",
    "web_search": "fetch",
    "web_fetch": "fetch",
    "Grep": "search",
    "Glob": "search",
}

# Fields from the stream's system/init event worth keeping. Account-bound
# fields (email, org, api key source detail) are deliberately not in this list.
_INIT_KEEP = ("model", "permissionMode", "claude_code_version", "tools", "output_style",
              "apiKeySource", "slash_commands", "agents", "mcp_servers")
_RESULT_KEEP = ("subtype", "duration_ms", "duration_api_ms", "total_cost_usd", "usage",
                "modelUsage", "permission_denials", "stop_reason")


class ClaudeCode(Adapter):
    name = "claude-code"
    provider = "anthropic"
    wire_format = "anthropic-messages"
    wire_hosts = ("api.anthropic.com",)
    noise_env: ClassVar[dict[str, str]] = {"DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
                 "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    credential_env = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")
    wrapper_execs = frozenset({"pwd", "cat", "rm", "mkdir"})  # cwd capture / snapshot plumbing

    def __init__(self, binary: str = "claude") -> None:
        self.binary = binary

    def version(self) -> str:
        path = shutil.which(self.binary)
        if not path:
            return "not-installed"
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30)
        return (r.stdout or r.stderr).strip().split(" ")[0]

    # ----------------------------------------------------------------- launch

    def prepare(self, trial: dict[str, Any], workdir: Path, context_dir: Path) -> LaunchSpec:
        arm = trial.get("arm") or {}
        self.plant(trial, workdir, context_dir)
        prompt = self.build_prompt(trial, context_dir)

        argv = [
            self.binary, "--print",
            "--output-format", "stream-json", "--verbose",
            "--input-format", "text",
            "--session-id", str(uuid.uuid4()),
            "--no-session-persistence",
        ]
        if trial.get("model"):
            argv += ["--model", trial["model"]]
        perm = arm.get("permission_mode") or trial.get("agent", {}).get("permission_mode")
        if perm:
            argv += ["--permission-mode", perm]
        for flag in ("allowed_tools", "disallowed_tools"):
            tools = arm.get(flag)
            if tools:
                argv += ["--" + flag.replace("_", "-"), ",".join(tools)]
        if arm.get("settings") is not None:
            settings_path = context_dir / "settings.json"
            settings_path.write_text(json.dumps(arm["settings"], indent=2))
            argv += ["--settings", str(settings_path)]
        # Only read settings from the project dir, so the image's (or the
        # operator's) user-level settings cannot leak into a trial.
        argv += ["--setting-sources", arm.get("setting_sources", "project")]
        argv += list(arm.get("extra_args") or [])

        self.record_argv(argv, context_dir)
        return LaunchSpec(argv=argv, stdin_text=prompt, env={}, cwd=workdir)

    # ------------------------------------------------------------------ tools

    def tool_kind(self, name: str) -> ToolKind:
        if name.startswith("mcp__"):
            return "mcp"
        return _KINDS.get(name, "other")

    def file_path(self, call: ToolCall) -> str | None:
        for k in ("file_path", "notebook_path", "path"):
            v = call.input.get(k)
            if isinstance(v, str):
                return v
        return None

    # ----------------------------------------------------------------- parsing

    def parse_stream(self, lines: Iterable[str]) -> StreamParse:
        events = read_json_lines(lines)

        out = StreamParse()
        seen_calls: set[str] = set()
        seen_results: set[str] = set()

        for i, ev in enumerate(events):
            et = ev.get("type")
            if et == "system" and ev.get("subtype") == "init":
                out.init = {k: ev[k] for k in _INIT_KEEP if k in ev}
                continue
            if et == "result":
                out.final = FinalReport(
                    text=ev.get("result"), is_error=bool(ev.get("is_error")),
                    num_turns=ev.get("num_turns"),
                    extra={k: ev[k] for k in _RESULT_KEEP if k in ev})
                continue

            raw_msg = ev.get("message")
            msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
            for blk in msg.get("content") or []:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_use" and blk.get("id") not in seen_calls:
                    seen_calls.add(blk["id"])
                    out.tool_calls.append(ToolCall(
                        tool_id=blk["id"], name=blk.get("name") or "",
                        kind=self.tool_kind(blk.get("name") or ""),
                        input=blk.get("input") or {}, source="stream"))
                elif blk.get("type") == "tool_result" and blk.get("tool_use_id") not in seen_results:
                    tid = blk.get("tool_use_id") or ""
                    seen_results.add(tid)
                    out.tool_results.append(ToolResult(
                        tool_id=tid, is_error=bool(blk.get("is_error")),
                        content=content_text(blk.get("content")),
                        following_text=_following_text(events, i)))
        return out


def _following_text(events: list[dict[str, Any]], i: int) -> str | None:
    """Assistant text after event i, up to the next tool call or the final result."""
    parts: list[str] = []
    for ev in events[i + 1:]:
        if ev.get("type") == "result":
            break
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message") or {}
        blocks = [b for b in msg.get("content") or [] if isinstance(b, dict)]
        if any(b.get("type") == "tool_use" for b in blocks):
            # Text in the same message, before the tool call, still counts.
            for b in blocks:
                if b.get("type") == "tool_use":
                    break
                if b.get("type") == "text" and b.get("text"):
                    parts.append(b["text"])
            break
        parts.extend(b["text"] for b in blocks if b.get("type") == "text" and b.get("text"))
    return "\n".join(parts) or None
