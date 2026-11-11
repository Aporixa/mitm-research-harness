"""Parse `strace -f -ttt` output into Exec / FileOp / NetConnect records.

Changes from v1:
  * Timestamps are epoch seconds (`-ttt`), not wall-clock HH:MM:SS. v1 had
    to guess the date and timezone from the API timestamp, which broke
    across midnight and whenever the VM and host clocks disagreed on zone.
  * clone/clone3/fork/vfork are traced so each exec carries its parent
    pid. The correlator uses the process tree to separate the agent's own
    housekeeping (git status, ripgrep) from tool-driven executions.
  * File reads are kept (mode="read"), since Read-type tools are checked
    against them; unlink/rename are recorded as writes.

The tokenizer accepts the three pid-prefix conventions strace emits and
stitches `<unfinished ...>` / `<... resumed>` pairs per pid.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from mrh.events import Exec, FileOp, NetConnect

# The strace invocation the harness uses. Kept here so parser and capture
# can never drift apart.
STRACE_SYSCALLS = (
    "execve,execveat,clone,clone3,fork,vfork,"
    "openat,open,creat,unlink,unlinkat,rename,renameat,renameat2,connect"
)
STRACE_ARGS = ["-f", "-ttt", "-s", "8192", "--quiet=attach,personality,path-resolution",
               "-e", f"trace={STRACE_SYSCALLS}"]

_RE_PID_BRACKET = re.compile(r"^\[pid\s+(\d+)\]\s+")
_RE_PID_BARE = re.compile(r"^(\d+)\s+(?=\d+\.\d+\s)")
_RE_TIME = re.compile(r"^(\d+\.\d+)\s+")

_RE_NORMAL = re.compile(r"^(?P<call>\w+)\((?P<args>.*)\)\s*=\s*(?P<ret>-?\d+|\?)(?P<tail>.*)$")
_RE_UNFINISHED = re.compile(r"^(?P<call>\w+)\((?P<args>.*?)\s*<unfinished\s*\.\.\.>\s*$")
_RE_RESUMED = re.compile(
    r"^<\.\.\.\s*(?P<call>\w+)\s+resumed>\s*(?P<args>.*)\)\s*=\s*(?P<ret>-?\d+|\?)(?P<tail>.*)$"
)


@dataclass
class Syscall:
    pid: int | None
    ts: float | None
    call: str
    args: str
    ret: int | None
    raw: str
    orphan: bool = False


def _split_prefix(line: str) -> tuple[int | None, float | None, str]:
    pid = None
    m = _RE_PID_BRACKET.match(line) or _RE_PID_BARE.match(line)
    if m:
        pid = int(m.group(1))
        line = line[m.end():]
    ts = None
    m = _RE_TIME.match(line)
    if m:
        ts = float(m.group(1))
        line = line[m.end():]
    return pid, ts, line


def _ret(s: str) -> int | None:
    return None if s == "?" else int(s)


def iter_syscalls(lines: Iterable[str]) -> Iterator[Syscall]:
    pending: dict[int | None, Syscall] = {}
    for raw in lines:
        line = raw.rstrip("\n")
        if not line:
            continue
        pid, ts, body = _split_prefix(line)

        m = _RE_RESUMED.match(body)
        if m:
            head = pending.pop(pid, None)
            if head is None or head.call != m.group("call"):
                yield Syscall(pid, ts, m.group("call"), m.group("args"), _ret(m.group("ret")),
                              line, orphan=True)
            else:
                yield Syscall(pid, head.ts, head.call, (head.args + " " + m.group("args")).strip(),
                              _ret(m.group("ret")), head.raw + " ... " + line)
            continue

        m = _RE_UNFINISHED.match(body)
        if m:
            pending[pid] = Syscall(pid, ts, m.group("call"), m.group("args"), None, line)
            continue

        m = _RE_NORMAL.match(body)
        if m:
            yield Syscall(pid, ts, m.group("call"), m.group("args"), _ret(m.group("ret")), line)
        # Anything else ("+++ exited with 0 +++", "--- SIGCHLD ...") carries
        # nothing the analyzers use.


# ---------------------------------------------------------------------------
# C-string decoding and argument parsing
# ---------------------------------------------------------------------------

_RE_ESCAPE = re.compile(r"\\(?:x[0-9a-fA-F]{2}|[0-7]{1,3}|.)")
_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "v": "\v", "f": "\f", "b": "\b", "a": "\a",
           "\\": "\\", '"': '"', "'": "'"}


def unescape(s: str) -> str:
    def one(m: re.Match[str]) -> str:
        seq = m.group(0)
        c = seq[1]
        if c == "x":
            return chr(int(seq[2:], 16))
        if c.isdigit():
            return chr(int(seq[1:], 8))
        return _SIMPLE.get(c, c)

    return _RE_ESCAPE.sub(one, s)


_RE_QSTRING = re.compile(r'"((?:[^"\\]|\\.)*)"(\.\.\.)?')


def split_top_level(args: str) -> list[str]:
    """Split a syscall argument list on commas outside quotes/brackets."""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = esc = False
    for ch in args:
        if esc:
            buf.append(ch)
            esc = False
            continue
        if in_str:
            buf.append(ch)
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return out


def _strings(s: str) -> list[tuple[str, bool]]:
    return [(unescape(m.group(1)), bool(m.group(2))) for m in _RE_QSTRING.finditer(s)]


def parse_exec_args(call: str, args: str) -> tuple[str, list[str], bool]:
    parts = split_top_level(args)
    if call == "execveat" and parts:
        parts = parts[1:]  # dirfd first
    binary_strs = _strings(parts[0]) if parts else []
    binary = binary_strs[0][0] if binary_strs else ""
    argv_strs = _strings(parts[1]) if len(parts) > 1 else []
    argv = [s for s, _ in argv_strs]
    # strace marks a clipped string with `"..."...` and a clipped array with
    # a trailing `...` element.
    truncated = any(t for _, t in binary_strs + argv_strs) or (
        len(parts) > 1 and parts[1].rstrip("]").rstrip().endswith("...")
    )
    return binary, argv, truncated


_READ_ONLY = {"O_RDONLY"}
_WRITE_FLAGS = {"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"}


def parse_open(call: str, args: str) -> tuple[str, list[str]]:
    parts = split_top_level(args)
    if call == "openat":
        parts = parts[1:]
    path_strs = _strings(parts[0]) if parts else []
    path = path_strs[0][0] if path_strs else ""
    if call == "creat":
        return path, ["O_CREAT", "O_WRONLY", "O_TRUNC"]
    flags = [t.strip() for t in parts[1].split("|")] if len(parts) > 1 else []
    return path, flags


_RE_FAMILY = re.compile(r"sa_family=(AF_\w+)")
_RE_PORT = re.compile(r"sin6?_port=htons\((\d+)\)")
_RE_ADDR4 = re.compile(r'inet_addr\("([^"]+)"\)')
_RE_ADDR6 = re.compile(r'inet_pton\(AF_INET6,\s*"([^"]+)"')
_RE_SUN = re.compile(r'sun_path=(?:@)?"([^"]*)"')


def parse_connect(args: str) -> tuple[str, str | None, int | None]:
    fam = _RE_FAMILY.search(args)
    family = fam.group(1) if fam else "unknown"
    port_m = _RE_PORT.search(args)
    port = int(port_m.group(1)) if port_m else None
    addr = None
    for rx in (_RE_ADDR4, _RE_ADDR6, _RE_SUN):
        m = rx.search(args)
        if m:
            addr = m.group(1)
            break
    return family, addr, port


# ---------------------------------------------------------------------------
# Whole-log parse
# ---------------------------------------------------------------------------

_FORKS = {"clone", "clone3", "fork", "vfork"}


@dataclass
class StraceParse:
    execs: list[Exec]
    files: list[FileOp]
    net: list[NetConnect]
    parents: dict[int, int]  # child pid -> parent pid
    root_pid: int | None
    orphans: int

    def ancestors(self, pid: int) -> list[int]:
        out: list[int] = []
        seen = {pid}
        while pid in self.parents:
            pid = self.parents[pid]
            if pid in seen:
                break
            seen.add(pid)
            out.append(pid)
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "execs": len(self.execs),
            "execs_ok": sum(1 for e in self.execs if e.ret == 0),
            "file_ops": len(self.files),
            "connects": len(self.net),
            "processes": len(self.parents) + (1 if self.root_pid is not None else 0),
            "orphan_resumes": self.orphans,
        }


def parse_strace(lines: Iterable[str]) -> StraceParse:
    execs: list[Exec] = []
    files: list[FileOp] = []
    net: list[NetConnect] = []
    parents: dict[int, int] = {}
    root: int | None = None
    orphans = 0

    for sc in iter_syscalls(lines):
        if sc.orphan:
            orphans += 1
        pid = sc.pid if sc.pid is not None else (root or 0)
        if root is None:
            root = pid
        ts = sc.ts or 0.0

        if sc.call in _FORKS:
            if sc.ret is not None and sc.ret > 0:
                parents[sc.ret] = pid
        elif sc.call in ("execve", "execveat"):
            binary, argv, truncated = parse_exec_args(sc.call, sc.args)
            execs.append(Exec(pid=pid, ppid=parents.get(pid), ts=ts, binary=binary, argv=argv,
                              ret=sc.ret, truncated=truncated))
        elif sc.call in ("open", "openat", "creat"):
            path, flags = parse_open(sc.call, sc.args)
            mode: Any = "write" if set(flags) & _WRITE_FLAGS else "read"
            files.append(FileOp(pid=pid, ts=ts, path=path, mode=mode, flags=flags, ret=sc.ret))
        elif sc.call in ("unlink", "unlinkat", "rename", "renameat", "renameat2"):
            strs = [s for s, _ in _strings(sc.args)]
            for p in strs:
                files.append(FileOp(pid=pid, ts=ts, path=p, mode="write", flags=[sc.call],
                                    ret=sc.ret))
        elif sc.call == "connect":
            family, addr, port = parse_connect(sc.args)
            net.append(NetConnect(pid=pid, ts=ts, family=family, address=addr, port=port,
                                  ret=sc.ret))

    return StraceParse(execs, files, net, parents, root, orphans)
