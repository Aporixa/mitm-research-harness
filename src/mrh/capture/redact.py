"""Capture-time credential redaction. Kept free of mitmproxy imports so it
can be unit-tested without the capture extra installed."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path

SENSITIVE_HEADERS = {
    "x-api-key",
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "anthropic-admin-api-key",
    "x-goog-api-key",
    "x-goog-user-project",
    "openai-organization",
    "openai-project",
    "chatgpt-account-id",
}

# Query parameters that carry credentials (the Gemini API accepts ?key=).
SENSITIVE_PARAMS = {"key", "api_key", "access_token", "token"}


def fingerprint(value: str) -> str:
    return "<redacted:sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12] + ">"


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: fingerprint(v) if k.lower() in SENSITIVE_HEADERS else v
            for k, v in headers.items()}


def redact_url(url: str) -> str:
    """Replace credential query parameters with their fingerprint."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    if not parts.query:
        return url
    q = [(k, fingerprint(v) if k.lower() in SENSITIVE_PARAMS else v)
         for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(q, safe="<>:")))


# Headers or query parameters that authenticate a model request. A session
# whose model requests carry none of these never sent its credential.
AUTH_HEADERS = {"x-api-key", "authorization", "x-goog-api-key"}


def carries_credential(rec: dict[str, object]) -> bool:
    headers = rec.get("headers") or {}
    if isinstance(headers, dict) and any(
            str(k).lower() in AUTH_HEADERS and str(v).strip() for k, v in headers.items()):
        return True
    return "<redacted:" in str(rec.get("url") or "")


# Providers echo the key back in error bodies ("Incorrect API key provided:
# sk-..."), so header redaction alone is not enough. After each session every
# output file is scrubbed of the credential values themselves, and of the
# masked forms some providers print (sk-ab12****wxyz).
_ECHO = re.compile(r"(?i)(api key provided:\s*)([^\s\"'\\,]+)")
MIN_SECRET_LEN = 8


def scrub_text(text: str, secrets: Iterable[str]) -> str:
    for s in sorted({s for s in secrets if len(s) >= MIN_SECRET_LEN}, key=len, reverse=True):
        text = text.replace(s, fingerprint(s))
    return _ECHO.sub(lambda m: m.group(1) + (m.group(2) if m.group(2).startswith("<redacted:")
                                             else "<redacted:echo>"), text)


def scrub_files(root: Path, secrets: Iterable[str]) -> list[str]:
    """Rewrite files under `root` that contain a secret or an echoed key.
    Returns the relative paths that changed."""
    secrets = list(secrets)
    changed = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        raw = p.read_bytes()
        text = raw.decode("utf-8", errors="surrogateescape")
        new = scrub_text(text, secrets)
        if new != text:
            p.write_bytes(new.encode("utf-8", errors="surrogateescape"))
            changed.append(str(p.relative_to(root)))
    return changed
