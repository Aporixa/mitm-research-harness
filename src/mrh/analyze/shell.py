"""Shell-command helpers: split compound commands, match them to execs.

v1 matched a Bash call to one exec by string comparison. That works for a
single command, but a compound such as `a && b | c` produces one wrapper
shell plus several children, and v1 could not say which of those ran.
v2 parses the command with bashlex, gets the list of simple commands, and
checks each one against the execs that descend from the matched wrapper
shell. When bashlex cannot parse a command (heredocs, some expansions), it
falls back to a conservative tokenizer and says so in the result.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Any

SHELL_BINARIES = {"sh", "bash", "dash", "zsh", "ash", "ksh", "busybox"}
# Shell builtins never show up as execve, so they are not expected to.
BUILTINS = {
    "echo", "printf", "cd", "export", "set", "unset", "source", ".", "eval", "exit", "true",
    "false", "test", "[", "read", "type", "alias", "shopt", "pwd", "wait", "trap", "return",
    "local", "declare", "let", ":", "builtin", "command", "exec", "ulimit", "umask", "times",
}


@dataclass
class SimpleCommand:
    name: str  # basename of argv[0]
    words: list[str]


@dataclass
class ShellSplit:
    commands: list[SimpleCommand]
    operators: list[str]  # "&&", "||", ";", "|", "&"
    parsed_with: str  # "bashlex" | "fallback"

    @property
    def is_compound(self) -> bool:
        return len(self.commands) > 1

    def expected_execs(self) -> list[str]:
        return [c.name for c in self.commands if c.name and c.name not in BUILTINS]


def split_command(cmd: str) -> ShellSplit:
    try:
        return _split_bashlex(cmd)
    except Exception:  # bashlex raises a range of internal errors
        return _split_fallback(cmd)


def _split_bashlex(cmd: str) -> ShellSplit:
    import bashlex

    commands: list[SimpleCommand] = []
    operators: list[str] = []

    class V(bashlex.ast.nodevisitor):
        def visitoperator(self, node: object, op: str) -> None:
            operators.append(op)

        def visitpipe(self, node: object, pipe: str) -> None:
            operators.append("|")

        def visitcommand(self, node: object, parts: list[Any]) -> None:
            words = [p.word for p in parts if getattr(p, "kind", "") == "word"]
            # Skip leading VAR=value assignments.
            while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
                words = words[1:]
            if words:
                commands.append(SimpleCommand(os.path.basename(words[0]), words))

    for tree in bashlex.parse(cmd):
        V().visit(tree)
    return ShellSplit(commands, [o for o in operators if o != "\n"], "bashlex")


_OPS = re.compile(r"(\|\||&&|;|\||&(?!>))")


def _split_fallback(cmd: str) -> ShellSplit:
    pieces = _OPS.split(cmd)
    commands: list[SimpleCommand] = []
    operators: list[str] = []
    for i, p in enumerate(pieces):
        if i % 2:
            operators.append(p)
            continue
        try:
            words = shlex.split(p, comments=True)
        except ValueError:
            words = p.split()
        if words:
            commands.append(SimpleCommand(os.path.basename(words[0]), words))
    return ShellSplit(commands, operators, "fallback")


def shell_script_of(binary: str, argv: list[str]) -> str | None:
    """If an exec is `<shell> [-l] -c <script> ...`, return the script."""
    if os.path.basename(binary) not in SHELL_BINARIES and (
        not argv or os.path.basename(argv[0]) not in SHELL_BINARIES):
        return None
    # Shells read all options before operands, so in `bash -c -l SCRIPT` the
    # script is the first non-option argument after -c, not the next argument.
    seen_c = False
    for a in argv[1:]:
        if a == "--":
            continue
        if a.startswith("-") or a.startswith("+"):
            if not a.startswith("--") and "c" in a[1:]:
                seen_c = True
            continue
        return a if seen_c else None
    return None


def normalize(s: str) -> str:
    s = s.replace("'\\''", "'")  # undo single-quote escaping used by wrappers
    return " ".join(s.split())


def match_score(requested: str, script: str, truncated: bool) -> int:
    """100 = request is contained in the exec'd script (word-bounded),
    75 = exec'd script was truncated by strace and is a prefix of the request,
    50 = first word matches, 0 = no match."""
    r, s = normalize(requested), normalize(script)
    if not r or not s:
        return 0
    if re.search(r"(?<![\w-])" + re.escape(r) + r"(?![\w-])", s):
        return 100
    if truncated:
        # The strace copy ends early; compare the overlapping prefix.
        tail = s.split("eval ", 1)[-1].lstrip("'\"")
        if len(tail) >= 8 and r.startswith(tail[: max(8, len(tail) - 3)]):
            return 75
    first = r.split(" ", 1)[0]
    if first and re.search(r"(?<![\w/-])" + re.escape(first) + r"(?![\w-])", s):
        return 50
    return 0
