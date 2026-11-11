from mrh.adapters.base import Adapter
from mrh.adapters.claude_code import ClaudeCode
from mrh.adapters.codex import Codex
from mrh.adapters.gemini_cli import GeminiCli

ADAPTERS: dict[str, type[Adapter]] = {
    "claude-code": ClaudeCode,
    "codex": Codex,
    "gemini-cli": GeminiCli,
}


def get_adapter(name: str) -> Adapter:
    try:
        return ADAPTERS[name]()
    except KeyError:
        raise SystemExit(f"unknown adapter {name!r}; known: {', '.join(ADAPTERS)}") from None
