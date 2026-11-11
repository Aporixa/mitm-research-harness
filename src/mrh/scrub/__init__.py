"""Turn a run directory into something safe to publish.

Per-file policy (anything not listed is dropped, so a new capture file is
private until someone adds it here on purpose):

  keep      copied as-is after a secret/PII scan
  scrub     copied with secrets, home paths, user and host names replaced
  derive    replaced by a reduced form (see each function)

After scrubbing, `gate()` rescans every output file. Any remaining hit
blocks export. v1 scrubbed with regexes but never rechecked its own output.

The gate is a backstop for the human review described in docs/ethics.md,
not a replacement for it.
"""

from __future__ import annotations

import fnmatch
import getpass
import json
import os
import re
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLICY: list[tuple[str, str]] = [
    # run level
    ("experiment.yaml", "keep"),
    ("run.json", "keep"),
    ("trials.jsonl", "scrub"),
    ("aggregate.json", "scrub"),
    ("summary.md", "scrub"),
    ("reviews.csv", "scrub"),
    ("reviews-*.csv", "scrub"),
    ("review_key.csv", "scrub"),
    # trial level
    ("trials/*/manifest.json", "derive"),
    ("trials/*/analysis.json", "scrub"),
    ("trials/*/stream.jsonl", "derive"),
    ("trials/*/wire.calls.jsonl", "scrub"),
    ("trials/*/context/prompt.txt", "keep"),
    ("trials/*/context/argv.json", "scrub"),
    ("trials/*/context/settings.json", "keep"),
    ("trials/*/context/gemini-settings.json", "keep"),
    ("trials/*/context/planted/*", "keep"),
    # Dropped on purpose: wire.jsonl (full system prompt and conversation),
    # strace.log (every path and argv on the box), stderr.log, mitm.log.
]

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_oauth", re.compile(r"\bya29\.[A-Za-z0-9_\-]{20,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("basic_auth_url", re.compile(r"[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
]
# Addresses that are allowed to appear (placeholders, vendor contacts in prompts).
EMAIL_ALLOW = re.compile(r"@(?:example\.(?:com|org|net)|users\.noreply\.github\.com)$")


@dataclass
class Identity:
    user: str
    host: str
    home: str

    @classmethod
    def current(cls, extra_users: list[str] | None = None) -> Identity:
        return cls(user=getpass.getuser(), host=socket.gethostname(),
                   home=os.path.expanduser("~"))


# Container users that say nothing about the operator. Redacting them would
# also rewrite ordinary words ("node", "agent") throughout the transcripts.
GENERIC_USERS = {"node", "agent", "root", "nobody", "runner", "ubuntu"}
# A home this generic says nothing about the operator, and replacing it would
# rewrite every /tmp path (in the container HOME=/tmp).
GENERIC_HOMES = {"", "/", "/tmp"}
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
TRIAL_DIR = re.compile(r"(?:/tmp/|~/)harness-trial-[A-Za-z0-9_]+")
# Structural leaks: not secrets, but they tie a dataset to one session or host.
STRUCTURAL_PATTERNS = [("session_uuid", UUID), ("trial_dir", re.compile(r"harness-trial-"))]


def scrub_text(text: str, ident: Identity, extra_terms: tuple[str, ...] | list[str] = ()) -> str:
    for name, rx in SECRET_PATTERNS:
        if name == "email":
            text = rx.sub(lambda m: m.group(0) if EMAIL_ALLOW.search(m.group(0))
                          else "<email>", text)
        else:
            text = rx.sub(f"<redacted:{name}>", text)
    # Trial dirs first: they may sit under HOME, and must not become ~/harness-trial-*.
    text = TRIAL_DIR.sub("/tmp/<trial>", text)
    if ident.home not in GENERIC_HOMES:
        text = text.replace(ident.home, "~")
    text = re.sub(r"/home/[^/\s\"']+", "/home/<user>", text)
    text = re.sub(r"/Users/[^/\s\"']+", "/Users/<user>", text)
    text = UUID.sub("<uuid>", text)
    for term in [_user(ident), ident.host, *extra_terms]:
        if term and len(term) >= 3:
            text = re.sub(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", "<redacted>", text)
    return text


def _user(ident: Identity) -> str:
    return "" if ident.user in GENERIC_USERS else ident.user


def _policy(rel: str) -> str:
    for pattern, action in POLICY:
        if fnmatch.fnmatch(rel, pattern):
            return action
    return "drop"


_INIT_DROP = {"session_id", "cwd", "original_cwd", "uuid", "apiKeySource", "mcp_servers", "plugins", "memory_paths"}


def derive_stream(text: str) -> str:
    """Keep the conversation events; drop session ids, cwd and similar
    account/host-bound fields from every event."""
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            for k in list(ev):
                if k in _INIT_DROP:
                    del ev[k]
        out.append(json.dumps(ev))
    return "\n".join(out) + "\n"


def derive_manifest(text: str) -> str:
    m: dict[str, Any] = json.loads(text)
    keep: dict[str, Any] = {k: m[k] for k in ("schema", "harness_version", "harness_commit",
                              "harness_src_hash", "scrubbed_files", "image", "adapter",
                              "adapter_experimental", "provider", "wire_format",
                              "agent_version", "trial",
                              "tools", "status", "exit_code", "duration_s", "started",
                              "finished") if k in m}
    keep["env_names"] = sorted((m.get("env") or {}).keys())
    keep["host"] = {"in_container": (m.get("host") or {}).get("in_container"),
                    "python": (m.get("host") or {}).get("python")}
    return json.dumps(keep, indent=2) + "\n"


def scrub_run(run_dir: Path, out: Path, ident: Identity | None = None,
              extra_terms: tuple[str, ...] | list[str] = ()) -> dict[str, list[str]]:
    ident = ident or Identity.current()
    if out.exists():
        raise SystemExit(f"{out} exists; refusing to merge into a previous scrub")
    report: dict[str, list[str]] = {"keep": [], "scrub": [], "derive": [], "drop": []}
    for src in sorted(run_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(run_dir).as_posix()
        action = _policy(rel)
        report[action].append(rel)
        if action == "drop":
            continue
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if action == "keep":
            shutil.copy2(src, dest)
            continue
        text = src.read_text(errors="replace")
        if action == "derive":
            text = derive_stream(text) if rel.endswith("stream.jsonl") else derive_manifest(text)
        dest.write_text(scrub_text(text, ident, list(extra_terms)))
    return report


def gate(out: Path, ident: Identity | None = None,
         extra_terms: tuple[str, ...] | list[str] = ()) -> list[dict[str, Any]]:
    """Rescan scrubbed output. Returns hits; empty means it passed."""
    ident = ident or Identity.current()
    hits = []
    home = ident.home if ident.home not in GENERIC_HOMES else ""
    terms = [t for t in (_user(ident), ident.host, home, *extra_terms) if t and len(t) >= 3]
    for p in sorted(out.rglob("*")):
        if not p.is_file():
            continue
        text = p.read_text(errors="replace")
        rel = p.relative_to(out).as_posix()
        for name, rx in SECRET_PATTERNS:
            for m in rx.finditer(text):
                if name == "email" and EMAIL_ALLOW.search(m.group(0)):
                    continue
                hits.append({"file": rel, "kind": name, "at": m.start()})
        for name, rx in STRUCTURAL_PATTERNS:
            found = rx.search(text)
            if found:
                hits.append({"file": rel, "kind": name, "at": found.start()})
        for t in terms:
            if re.search(r"(?<![\w-])" + re.escape(t) + r"(?![\w-])", text):
                hits.append({"file": rel, "kind": "identity_term", "term_len": len(t)})
    return hits
