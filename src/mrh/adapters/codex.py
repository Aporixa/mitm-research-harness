"""Adapter for OpenAI Codex CLI (`codex exec --json`).

EXPERIMENTAL: written against the published `codex exec --json` event
format and the Responses API, and tested on fixtures only. Check one
captured session by hand before using it in an experiment.

Things that differ from Claude Code and matter for the analysis:
  * Stream item ids (`item_3`) are not the wire call ids (`call_...`), so
    `ids_shared = False` and the correlator pairs calls by command and order.
  * The `shell` tool takes an argv list (`["bash", "-lc", "ls"]`). Codex may
    exec that argv directly, so a call can match either a `<shell> -c`
    wrapper or a direct exec of the same argv.
  * Codex runs commands under its own sandbox (Landlock/seccomp on Linux).
    In a container that sandbox can fail to start, which looks like a
    refusal. Set the arm's `sandbox` explicitly and record it.
  * Authentication: `codex exec` reads CODEX_API_KEY, not OPENAI_API_KEY.
    OPENAI_API_KEY is copied to CODEX_API_KEY for the agent when the latter
    is unset (see `credential_aliases`).
  * Codex talks to /v1/responses over a WebSocket first and falls back to
    HTTPS. The capture addon records WebSocket messages for this reason.
  * Telemetry is off: analytics and every OTel exporter are disabled with
    `-c` overrides (`_NO_TELEMETRY`), recorded in the argv.
  * The stream reports no cost; the max-cost guard in the runner cannot
    see Codex spend.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any, ClassVar

from mrh.adapters.base import Adapter, LaunchSpec, StreamParse, read_json_lines
from mrh.events import FinalReport, ToolCall, ToolKind, ToolResult

_KINDS: dict[str, ToolKind] = {
    "shell": "shell",
    "shell_command": "shell",
    "exec_command": "shell",
    # Code mode: the model writes JavaScript that calls tools.exec_command
    # (and other tools). Seen with gpt-6-astra on codex 0.166.
    "exec": "shell",
    "local_shell": "shell",
    "container.exec": "shell",
    "apply_patch": "file_edit",
    "view_image": "file_read",
    "web_search": "fetch",
    "update_plan": "other",
    "write_stdin": "other",
}
_CODE_CMD = re.compile(
    r"""exec_command\(\s*\{[^}]*?\bcmd\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)""",
    re.S)


def code_mode_commands(script: object) -> list[str]:
    """Commands passed to tools.exec_command in a code-mode `exec` script."""
    if not isinstance(script, str):
        return []
    out = []
    for lit in _CODE_CMD.findall(script):
        quote, body = lit[0], lit[1:-1]
        if quote == "`":  # template literal: taken as written
            out.append(body)
            continue
        if quote == "'":  # re-quote as a JSON string: \' -> ', bare " -> \"
            body = re.sub(r'\\\'|(?<!\\)"', lambda m: "'" if m.group(0) != '"' else '\\"', body)
        try:
            out.append(json.loads('"' + body + '"'))
        except ValueError:
            out.append(body)
    return out


# Codex sent OTLP metrics to ab.chatgpt.com in the first real-key run.
_NO_TELEMETRY: dict[str, str] = {
    "analytics.enabled": "false",
    "otel.exporter": "none",
    "otel.trace_exporter": "none",
    "otel.metrics_exporter": "none",
}

_PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)


class Codex(Adapter):
    name = "codex"
    provider = "openai"
    wire_format = "openai"
    wire_hosts = ("api.openai.com", "chatgpt.com")
    experimental = True
    credential_env = ("OPENAI_API_KEY", "CODEX_API_KEY")
    # `codex exec` authenticates from CODEX_API_KEY (or a login stored in
    # CODEX_HOME); it ignores OPENAI_API_KEY. Seen in the first dummy-key
    # capture: OPENAI_API_KEY set, requests sent with no Authorization header.
    credential_aliases: ClassVar[dict[str, str]] = {"CODEX_API_KEY": "OPENAI_API_KEY"}
    passthrough_env = (*Adapter.passthrough_env, "OPENAI_BASE_URL")
    ids_shared = False

    def __init__(self, binary: str = "codex") -> None:
        self.binary = binary

    def proxy_env(self, proxy_url: str, ca_cert: Path) -> dict[str, str]:
        # Codex is a Rust binary; SSL_CERT_FILE covers native-tls/rustls with
        # native roots, CODEX_CA_CERTIFICATE is Codex's own override.
        return super().proxy_env(proxy_url, ca_cert) | {"CODEX_CA_CERTIFICATE": str(ca_cert)}

    def version(self) -> str:
        path = shutil.which(self.binary)
        if not path:
            return "not-installed"
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30)
        out = (r.stdout or r.stderr).strip().split()
        return out[-1] if out else "unknown"

    def prepare(self, trial: dict[str, Any], workdir: Path, context_dir: Path) -> LaunchSpec:
        arm = trial.get("arm") or {}
        self.plant(trial, workdir, context_dir)
        prompt = self.build_prompt(trial, context_dir)
        argv = [self.binary, "exec", "--json", "--skip-git-repo-check", "--color", "never",
                "--cd", str(workdir)]
        if trial.get("model"):
            argv += ["--model", trial["model"]]
        sandbox = arm.get("sandbox") or (trial.get("agent") or {}).get("sandbox")
        if sandbox:
            argv += ["--sandbox", sandbox]
        # Telemetry off unless the arm overrides it. Keys checked against
        # codex 0.166 (`codex -c ... login status` rejects unknown values).
        for k, v in (_NO_TELEMETRY | (arm.get("config") or {})).items():
            argv += ["-c", f"{k}={v}"]
        argv += list(arm.get("extra_args") or [])
        argv.append("-")  # read the prompt from stdin
        self.record_argv(argv, context_dir)
        # CODEX_HOME under the per-trial HOME keeps operator config out.
        return LaunchSpec(argv=argv, stdin_text=prompt, env={}, cwd=workdir)

    # ------------------------------------------------------------------ tools

    def tool_kind(self, name: str) -> ToolKind:
        if name.startswith("mcp__") or ("." in name and name not in _KINDS):
            return "mcp"
        return _KINDS.get(name, "other")

    def shell_command(self, call: ToolCall) -> str | None:
        if call.kind == "shell" and call.name == "exec":
            cmds = code_mode_commands(call.input.get("input"))
            # One script may run several commands; pairing is one wire call
            # to one stream item, so only the first is used as the target.
            return cmds[0] if cmds else None
        return super().shell_command(call)

    def shell_argv(self, call: ToolCall) -> list[str] | None:
        if call.kind != "shell":
            return None
        cmd = call.input.get("command")
        if isinstance(cmd, list) and all(isinstance(x, str) for x in cmd):
            return list(cmd)
        if call.source == "stream" and isinstance(cmd, str):
            # The stream reports the argv shell-joined.
            try:
                return shlex.split(cmd)
            except ValueError:
                return None
        return None

    def file_path(self, call: ToolCall) -> str | None:
        for k in ("path", "file_path"):
            v = call.input.get(k)
            if isinstance(v, str):
                return v
        patch = call.input.get("input") or call.input.get("patch")
        if isinstance(patch, str):
            m = _PATCH_PATH.search(patch)
            if m:
                return m.group(1).strip()
        return None

    # ----------------------------------------------------------------- parsing

    def parse_stream(self, lines: Iterable[str]) -> StreamParse:
        events = read_json_lines(lines)
        out = StreamParse()
        messages: list[tuple[int, str]] = []  # (event index, text)
        result_at: list[tuple[int, ToolResult]] = []
        usage: dict[str, Any] = {}
        failed: str | None = None
        seen: set[str] = set()

        for i, ev in enumerate(events):
            et = ev.get("type")
            if et == "thread.started":
                out.init["thread_id"] = ev.get("thread_id")
                continue
            if et == "turn.completed":
                usage = ev.get("usage") or {}
                continue
            if et in ("turn.failed", "error"):
                err = ev.get("error") or {}
                failed = (err.get("message") if isinstance(err, dict) else None) or ev.get("message") or et
                continue
            if et != "item.completed":
                continue
            item = ev.get("item") or {}
            it, iid = item.get("type"), str(item.get("id") or f"item@{i}")
            if iid in seen:
                continue
            seen.add(iid)
            if it == "agent_message":
                messages.append((i, item.get("text") or ""))
            elif it == "command_execution":
                cmd = item.get("command") or ""
                out.tool_calls.append(ToolCall(tool_id=iid, name="shell", kind="shell",
                                               input={"command": cmd}, source="stream"))
                status = item.get("status")
                code = item.get("exit_code")
                r = ToolResult(tool_id=iid,
                               is_error=status in ("failed", "declined") or (code not in (0, None)),
                               content=item.get("aggregated_output") or "")
                out.tool_results.append(r)
                result_at.append((i, r))
            elif it == "file_change":
                changes = item.get("changes") or []
                first = next((c.get("path") for c in changes if isinstance(c, dict)), None)
                out.tool_calls.append(ToolCall(tool_id=iid, name="apply_patch", kind="file_edit",
                                               input={"path": first, "changes": changes},
                                               source="stream"))
                r = ToolResult(tool_id=iid, is_error=item.get("status") == "failed", content="")
                out.tool_results.append(r)
                result_at.append((i, r))
            elif it == "mcp_tool_call":
                name = f"mcp__{item.get('server', '')}__{item.get('tool', '')}"
                out.tool_calls.append(ToolCall(tool_id=iid, name=name, kind="mcp",
                                               input=item.get("arguments") or {}, source="stream"))
            elif it == "web_search":
                out.tool_calls.append(ToolCall(tool_id=iid, name="web_search", kind="fetch",
                                               input={"query": item.get("query")}, source="stream"))

        # following_text: agent messages after a result, up to the next tool item.
        tool_idx = sorted(i for i, _ in result_at)
        for i, r in result_at:
            nxt = next((j for j in tool_idx if j > i), len(events))
            txt = [t for j, t in messages if i < j < nxt and t]
            r.following_text = "\n".join(txt) or None

        final_text = messages[-1][1] if messages else None
        out.final = FinalReport(text=final_text, is_error=failed is not None,
                                extra={"usage": usage, **({"error": failed} if failed else {})})
        return out
