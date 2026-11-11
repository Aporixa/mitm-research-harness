"""Package a scrubbed run as a versioned, checksummed dataset.

    <dataset>/
        DATASET_CARD.md      filled from docs/dataset-card.md and the run
        MANIFEST.json        schema, harness version + git commit, agent version,
                             models, counts, sha256 of every file
        data/                the scrubbed run

Export refuses to proceed unless the scrub gate passes on the input, and
unless `--reviewed` is given: an explicit statement that a person has
read the material (docs/ethics.md).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from mrh import SCHEMA_VERSION, __version__
from mrh.scrub import gate
from mrh.util import sha256_file, write_json_atomic, write_text_atomic

# Source checkout: docs/dataset-card.md. Installed wheel: packaged next to
# this module (pyproject force-include).
_CARD_CANDIDATES = (Path(__file__).with_name("card_template.md"),
                    Path(__file__).resolve().parents[3] / "docs" / "dataset-card.md")


def _card_template() -> str:
    for p in _CARD_CANDIDATES:
        if p.exists():
            return p.read_text()
    return "# Dataset card\n"


def _git_commit() -> str | None:
    if os.environ.get("HARNESS_GIT_COMMIT"):
        return os.environ["HARNESS_GIT_COMMIT"]  # baked into the image
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                           cwd=Path(__file__).parent, timeout=10)
    except OSError:
        return None
    return r.stdout.strip() or None


def export(scrubbed: Path, out: Path, version: str, reviewed: bool,
           extra_terms: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    if not reviewed:
        raise SystemExit("export requires --reviewed: confirm a person has read every file "
                         "in the scrubbed directory (see docs/ethics.md).")
    hits = gate(scrubbed, extra_terms=list(extra_terms))
    if hits:
        raise SystemExit(f"scrub gate failed with {len(hits)} hits; first: {hits[:5]}")
    if out.exists():
        raise SystemExit(f"{out} exists; datasets are immutable, pick a new version")

    shutil.copytree(scrubbed, out / "data")
    run = json.loads((scrubbed / "run.json").read_text()) if (scrubbed / "run.json").exists() else {}
    agg_path = scrubbed / "aggregate.json"
    agg = json.loads(agg_path.read_text()) if agg_path.exists() else {}

    versions: dict[str, Any] = run.get("agent_versions_found") or {
        run.get("adapter") or "?": run.get("agent_version_found")}
    files = {p.relative_to(out).as_posix(): sha256_file(p)
             for p in sorted((out / "data").rglob("*")) if p.is_file()}
    manifest = {
        "dataset_version": version,
        "schema": SCHEMA_VERSION,
        "harness_version": __version__,
        "harness_commit": _git_commit(),
        "experiment": run.get("experiment"),
        "spec_hash": run.get("spec_hash"),
        "adapters": run.get("adapters") or [run.get("adapter")],
        "agent_versions": versions,
        "agent_version": run.get("agent_version_found"),
        "version_drift": run.get("version_drift"),
        "models": agg.get("models_seen"),
        "trials": agg.get("trials"),
        "human_reviewed_trials": agg.get("reviews"),
        "exported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
    }
    write_json_atomic(out / "MANIFEST.json", manifest)

    card = _card_template()
    for k, v in {"{{experiment}}": run.get("experiment"), "{{version}}": version,
                 "{{agent_version}}": ", ".join(
                     f"{n} {ver}" for n, ver in versions.items()),
                 "{{models}}": ", ".join(agg.get("models_seen") or []),
                 "{{trials}}": agg.get("trials"), "{{reviewed}}": agg.get("reviews"),
                 "{{exported}}": manifest["exported"],
                 "{{commit}}": manifest["harness_commit"]}.items():
        card = card.replace(k, str(v))
    write_text_atomic(out / "DATASET_CARD.md", card)
    return manifest
