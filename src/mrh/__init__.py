"""mitm-research-harness (mrh): instrumentation for coding agents from
any lab. Adapters ship for Claude Code (Anthropic), Codex CLI (OpenAI)
and Gemini CLI (Google); a new agent is one subclass of
mrh.adapters.base.Adapter, and a new API format one module in
mrh.analyze.wire.

Four records are captured for every trial and compared:

    asked     the prompt and standing context (pre-prompt, planted project files, settings)
    emitted   the tool calls the model sent, as seen on the wire (mitmproxy)
    executed  what the operating system actually ran (strace)
    reported  what the agent told its consumer (stream output / final result)

A disagreement between any two is a candidate finding. Candidates are
reviewed by hand; the analyzers never decide on their own that a model
fabricated something.
"""

__version__ = "2.0.0.dev0"

# Bumped whenever the shape of any normalized record or dataset file changes.
# Recorded in every session manifest and dataset manifest.
SCHEMA_VERSION = "2.0"
