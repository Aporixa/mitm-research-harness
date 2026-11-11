"""Model-id normalization.

The same model shows up under several spellings: a dated snapshot on the
wire (`claude-sonnet-5-5-20261001`), an alias in the request, a context
tag added by the agent (`claude-opus-5-5[1m]`), a resource path on Gemini
(`models/gemini-2.5-pro`), or an OpenAI snapshot date
(`gpt-5-codex-2026-09-15`). Comparing raw strings reports all of these as
"ran on a different model". Comparisons go through `normalize_model`;
the raw ids are kept alongside.
"""

from __future__ import annotations

import re

_TAG = re.compile(r"\[[^\]]*\]$")
_DATE_COMPACT = re.compile(r"-(?:19|20)\d{6}$")
_DATE_DASHED = re.compile(r"-(?:19|20)\d{2}-\d{2}-\d{2}$")
_PREFIXES = ("models/", "publishers/google/models/", "anthropic/", "openai/", "google/")


def normalize_model(model: str | None) -> str | None:
    if not model:
        return None
    m = model.strip().lower()
    for p in _PREFIXES:
        if m.startswith(p):
            m = m[len(p):]
    m = _TAG.sub("", m)
    m = _DATE_COMPACT.sub("", m)
    m = _DATE_DASHED.sub("", m)
    m = re.sub(r"-latest$", "", m)
    return m or None


def same_model(a: str | None, b: str | None) -> bool:
    return normalize_model(a) == normalize_model(b)
