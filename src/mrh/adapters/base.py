"""Agent adapter interface.

An adapter knows five things about one agent:
  1. how to launch it headless for a single trial (command, stdin, env, workdir setup)
  2. which hosts its model traffic goes to, which API format that traffic
     uses, and how to make the agent's runtime trust the capture proxy
  3. how to parse its consumer-facing output into normalized events
  4. how to classify its tools (shell, file write, fetch, ...) and where in
     a tool's input the target lives (command string or argv, file path, URL)
  5. which credentials it reads, by environment-variable name

Everything downstream (correlation, candidate detection, aggregation,
export) works on the normalized events and never imports an adapter.

Adding an agent means writing one subclass. The shipped adapters:

  claude-code   Anthropic Claude Code       verified against live captures (v1); Stage-0 pilot passed
  codex         OpenAI Codex CLI            Stage-0 pilot passed (docs/validation.md)
  gemini-cli    Google Gemini CLI           experimental: see docs/validation.md
"""

from __future__ import annotations

import json
import shlex
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from mrh.analyze.wire import PARSERS, WireParse
from mrh.events import FinalReport, ToolCall, ToolKind, ToolResult


@dataclass
class LaunchSpec:
    argv: list[str]
    stdin_text: str | None
    env: dict[str, str]
    cwd: Path
    # Files to write under the trial's fresh $HOME before launch, as
    # {path relative to $HOME: content}. For agent config that must live in
    # the user's config dir rather than the workdir.
    home_files: dict[str, str] = field(default_factory=dict)


@dataclass
class StreamParse:
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    final: FinalReport | None = None
    # Agent-reported session facts (model, permission mode, tool list, version).
    init: dict[str, Any] = field(default_factory=dict)


def read_json_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text") or json.dumps(b, default=str))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return json.dumps(content, default=str)


class Adapter(ABC):
    name: str
    provider: str  # "anthropic" | "openai" | "google" | ...
    wire_format: str  # key into mrh.analyze.wire.PARSERS
    wire_hosts: tuple[str, ...]
    # True until the adapter has been checked against a live capture.
    experimental: bool = False
    # Environment variables passed through to the agent. Values of names
    # in `credential_env` are never written to disk; only their presence.
    passthrough_env: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")
    credential_env: tuple[str, ...] = ()
    # Credential names the agent reads under a different name than the one
    # the operator set: {name the agent reads: name to take it from}. Applied
    # only when the agent's own name is not set.
    credential_aliases: ClassVar[dict[str, str]] = {}
    # Env that switches off agent traffic which is not part of the experiment
    # (auto-update, telemetry, error reporting). Recorded in the manifest.
    noise_env: ClassVar[dict[str, str]] = {}
    # Executables the agent's own tool wrapper runs around every shell
    # call (snapshot sourcing, cwd capture). Not counted as unrequested.
    wrapper_execs: frozenset[str] = frozenset()
    # Whether stream tool ids equal wire tool ids. When False the correlator
    # pairs stream and wire calls by kind, target and order instead.
    ids_shared: bool = True

    # ------------------------------------------------------------ capture env

    def proxy_env(self, proxy_url: str, ca_cert: Path) -> dict[str, str]:
        """Env that routes the agent through the proxy and makes it trust the
        per-trial CA. The default covers Node (NODE_EXTRA_CA_CERTS), OpenSSL
        and rustls-native-certs (SSL_CERT_FILE), Python requests and curl.
        Override for runtimes that read something else."""
        ca = str(ca_cert)
        return {"HTTPS_PROXY": proxy_url, "https_proxy": proxy_url,
                "HTTP_PROXY": proxy_url, "http_proxy": proxy_url,
                "NO_PROXY": "", "no_proxy": "",
                "NODE_EXTRA_CA_CERTS": ca, "SSL_CERT_FILE": ca,
                "REQUESTS_CA_BUNDLE": ca, "CURL_CA_BUNDLE": ca}

    # ----------------------------------------------------------------- launch

    @abstractmethod
    def version(self) -> str: ...

    @abstractmethod
    def prepare(self, trial: dict[str, Any], workdir: Path, context_dir: Path) -> LaunchSpec:
        """Set up `workdir` (planted files) and return how to launch the agent.

        Anything planted is also copied into `context_dir`, so the session
        records exactly what the agent could see.
        """

    def plant(self, trial: dict[str, Any], workdir: Path, context_dir: Path) -> None:
        context_dir.mkdir(parents=True, exist_ok=True)
        for rel, text in (trial.get("plant") or {}).items():
            dest = (workdir / rel).resolve()
            if workdir.resolve() not in dest.parents:
                raise ValueError(f"planted file escapes workdir: {rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
            (context_dir / "planted" / rel).parent.mkdir(parents=True, exist_ok=True)
            (context_dir / "planted" / rel).write_text(text)

    def build_prompt(self, trial: dict[str, Any], context_dir: Path) -> str:
        arm = trial.get("arm") or {}
        prompt: str = trial["prompt"]["text"]
        if arm.get("preprompt"):
            prompt = arm["preprompt"].rstrip() + "\n\n" + prompt
        (context_dir / "prompt.txt").write_text(prompt)
        return prompt

    @staticmethod
    def record_argv(argv: list[str], context_dir: Path) -> None:
        (context_dir / "argv.json").write_text(json.dumps(argv, indent=2))

    # ------------------------------------------------------------------ tools

    @abstractmethod
    def tool_kind(self, name: str) -> ToolKind: ...

    def shell_argv(self, call: ToolCall) -> list[str] | None:
        """argv for shells that take a list (`["bash", "-lc", "..."]` or a
        bare `["ls", "-la"]`). None for string-command shells."""
        return None

    def shell_command(self, call: ToolCall) -> str | None:
        """The command as one string. For argv-style shells this is the
        script of a `<shell> -c` argv, else the argv joined."""
        if call.kind != "shell":
            return None
        argv = self.shell_argv(call)
        if argv:
            from mrh.analyze.shell import shell_script_of

            script = shell_script_of(argv[0], argv)
            return script if script is not None else shlex.join(argv)
        for k in ("command", "cmd"):
            v = call.input.get(k)
            if isinstance(v, str):
                return v
        return None

    @abstractmethod
    def file_path(self, call: ToolCall) -> str | None: ...

    # ----------------------------------------------------------------- parsing

    @abstractmethod
    def parse_stream(self, lines: Iterable[str]) -> StreamParse: ...

    def parse_wire(self, records: Iterable[dict[str, Any]]) -> WireParse:
        return PARSERS[self.wire_format](records, self.tool_kind)
