from __future__ import annotations

import hashlib
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any


@contextmanager
def atomic_write(path: Path | str, mode: str = "w") -> Iterator[IO[Any]]:
    """Write to `<path>.tmp` and rename into place only on success.

    A crash mid-write leaves the previous file intact instead of a partial
    one that a later analysis pass would treat as complete.
    """
    final = Path(path)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(final.name + ".tmp")
    try:
        with tmp.open(mode) as f:
            yield f
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def free_port(lo: int = 18800, hi: int = 18999) -> int:
    """Return a port in [lo, hi] that is free right now.

    The caller still has to confirm its own process bound the port; there is
    an unavoidable window between this check and the real bind.
    """
    for port in range(lo, hi + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {lo}-{hi}")


def write_text_atomic(path: Path | str, text: str) -> None:
    with atomic_write(path) as f:
        f.write(text)


def write_json_atomic(path: Path | str, obj: Any) -> None:
    import json

    write_text_atomic(path, json.dumps(obj, indent=2, default=str) + "\n")
