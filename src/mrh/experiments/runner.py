"""Run an experiment's trial matrix, resumably.

Run directory:

    <out>/<experiment-id>/
        experiment.yaml       copy of the spec as run
        run.json              spec hash, agent version, start time
        trials.jsonl          one summary row per finished trial (append-only)
        trials/<trial_id>/    session directory (see harness.capture.session)

A trial whose directory already holds analysis.json is skipped, so a
crashed run continues where it stopped. A rerun with a changed spec is
refused, since it would mix two experiments under one id.

Guards, so a broken setup does not burn a budget:
  * A trial that raises is recorded as a `harness_error` row and the run
    moves on; it is retried on the next invocation.
  * After `max_consecutive_failures` trials in a row that raised or had
    capture problems (bad key, proxy not trusted, sandbox failing), the
    run stops.
  * With `max_cost_usd`, the run stops once the agent-reported cost of
    this experiment's trials reaches it. Adapters whose stream reports no
    cost (Codex, Gemini CLI) are not covered; the run says so.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from mrh.adapters import get_adapter
from mrh.adapters.base import Adapter
from mrh.analyze.session import analyze_session
from mrh.capture.session import run_session
from mrh.experiments.spec import Experiment
from mrh.util import write_json_atomic


class RunAborted(SystemExit):
    pass


def _cost(analysis: dict[str, Any]) -> float | None:
    v = (analysis.get("final") or {}).get("total_cost_usd")
    return float(v) if isinstance(v, int | float) else None


def summary_row(trial: dict[str, Any], manifest: dict[str, Any],
                analysis: dict[str, Any]) -> dict[str, Any]:
    ref = analysis.get("refusal") or {}
    out = analysis.get("outcome") or {}
    return {
        "trial_id": trial["trial_id"],
        "adapter": manifest.get("adapter") or trial.get("adapter"),
        "provider": manifest.get("provider"),
        "model_requested": trial["model"],
        "model_observed": analysis.get("model"),
        "model_normalized": analysis.get("model_normalized"),
        "model_matches_request": analysis.get("model_matches_request"),
        "models_side": analysis.get("models_side") or [],
        "arm": trial["arm"]["id"],
        "prompt": trial["prompt"]["id"],
        "tags": trial.get("tags") or {},
        "rep": trial["rep"],
        "status": manifest.get("status"),
        "capture_ok": analysis.get("capture_ok"),
        "capture_problems": analysis.get("capture_problems") or [],
        "agent_version": manifest.get("agent_version"),
        "refusal_label": ref.get("label"),
        "needs_review": ref.get("needs_review"),
        "refused_calls": ref.get("refused_calls", 0),
        "post_refusal_exec": ref.get("post_refusal_exec"),
        "route": out.get("route"),
        "effect_achieved": out.get("effect_achieved"),
        "reported_done": out.get("reported_done"),
        "workarounds": len(analysis["correlation"].get("workarounds") or []),
        "cost_usd": _cost(analysis),
        "verdict_counts": analysis.get("verdict_counts"),
        "wire_only": len(analysis["correlation"]["wire_only"]),
        "stream_only": len(analysis["correlation"]["stream_only"]),
        "input_diff": len(analysis["correlation"]["input_diff"]),
        "finished": time.time(),
    }


def error_row(trial: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    return {"trial_id": trial["trial_id"], "adapter": trial.get("adapter"),
            "model_requested": trial["model"], "arm": trial["arm"]["id"],
            "prompt": trial["prompt"]["id"], "tags": trial.get("tags") or {},
            "rep": trial["rep"], "status": "harness_error", "capture_ok": False,
            "capture_problems": ["harness_error"], "error": repr(exc)[:500],
            "finished": time.time()}


def _append(path: Path, row: dict[str, Any]) -> None:
    with path.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(row, default=str) + "\n")
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _prior_cost(path: Path) -> float:
    total = 0.0
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                total += float(json.loads(line).get("cost_usd") or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return total


def run(exp: Experiment, spec_path: Path, out_root: Path, limit: int | None = None,
        allow_version_drift: bool = False, only: str | None = None,
        max_consecutive_failures: int = 3, max_cost_usd: float | None = None) -> Path:
    adapters: dict[str, Adapter] = {}
    versions: dict[str, str] = {}
    for name in exp.adapters():
        adapters[name] = get_adapter(name)
        versions[name] = adapters[name].version()
        pinned = exp.pinned_version(name)
        if pinned and versions[name] != pinned and not allow_version_drift:
            raise SystemExit(f"spec pins {name} {pinned}, found {versions[name]}. "
                             "Rebuild the image for that version or pass --allow-version-drift "
                             "(the drift is recorded in run.json).")
        if adapters[name].experimental:
            print(f"warning: adapter {name} is experimental (not yet through the Stage-0 pilot); "
                  "check one session by hand before trusting results", file=sys.stderr)
        if max_cost_usd is not None and name != "claude-code":
            print(f"warning: {name} reports no cost; --max-cost does not cover it",
                  file=sys.stderr)

    run_dir = out_root / exp.id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_path = run_dir / "run.json"
    drift = {n: v for n, v in versions.items()
             if exp.pinned_version(n) and v != exp.pinned_version(n)}
    if meta_path.exists():
        prior = json.loads(meta_path.read_text())
        if prior.get("spec_hash") != exp.spec_hash():
            raise SystemExit(f"{run_dir} was started with a different spec "
                             f"({prior.get('spec_hash')} != {exp.spec_hash()}). "
                             "Use a new --out or a new experiment id.")
    else:
        shutil.copy(spec_path, run_dir / "experiment.yaml")
        write_json_atomic(meta_path, {
            "experiment": exp.id, "spec_hash": exp.spec_hash(), "started": time.time(),
            "adapters": exp.adapters(),
            "agent_versions_pinned": {n: exp.pinned_version(n) for n in exp.adapters()},
            "agent_versions_found": versions, "version_drift": drift,
            # kept for single-adapter readers
            "adapter": exp.adapter, "agent_version_pinned": exp.agent_version,
            "agent_version_found": versions.get(exp.adapter)})

    rows_path = run_dir / "trials.jsonl"
    spent = _prior_cost(rows_path)
    trials = exp.trials()
    if only:
        trials = [t for t in trials if only in t["trial_id"]]
    done = 0
    failures = 0
    for i, trial in enumerate(trials, 1):
        tdir = run_dir / "trials" / trial["trial_id"]
        if (tdir / "analysis.json").exists():
            continue
        if limit is not None and done >= limit:
            break
        if max_cost_usd is not None and spent >= max_cost_usd:
            raise RunAborted(f"stopped: reported cost ${spent:.2f} reached --max-cost "
                             f"${max_cost_usd:.2f}. Rerun with a higher limit to continue.")
        if tdir.exists():
            shutil.rmtree(tdir)  # partial capture from a crash; start clean
        print(f"[{i}/{len(trials)}] {trial['trial_id']}", file=sys.stderr, flush=True)
        adapter = adapters[trial.get("adapter") or exp.adapter]
        try:
            manifest = run_session(adapter, trial, tdir, timeout_s=exp.timeout_s)
            analysis = analyze_session(tdir, trial.get("expected_output"),
                                       trial.get("expected_pattern"))
        except Exception as exc:  # one broken trial must not end the run
            _append(rows_path, error_row(trial, exc))
            print(f"  harness error: {exc!r}", file=sys.stderr)
            ok = False
        else:
            row = summary_row(trial, manifest, analysis)
            _append(rows_path, row)
            spent += row.get("cost_usd") or 0.0
            ok = bool(analysis.get("capture_ok"))
            if not ok:
                print(f"  capture problems: {', '.join(analysis.get('capture_problems') or [])}",
                      file=sys.stderr)
        done += 1
        failures = 0 if ok else failures + 1
        if max_consecutive_failures and failures >= max_consecutive_failures:
            raise RunAborted(f"stopped: {failures} trials in a row failed or had capture "
                             f"problems. Check {tdir} (stderr.log, mitm.log) before rerunning.")
    return run_dir


def reanalyze(run_dir: Path) -> int:
    """Re-run analysis over every captured trial (after an analyzer change)."""
    rows = []
    for tdir in sorted((run_dir / "trials").iterdir()):
        mpath = tdir / "manifest.json"
        if not mpath.exists():
            continue
        manifest = json.loads(mpath.read_text())
        trial = manifest["trial"] | {"arm": {"id": manifest["trial"]["arm"]}}
        if manifest.get("status") == "harness_error":
            rows.append(error_row(trial, RuntimeError(manifest.get("error", "harness_error"))))
            continue
        analysis = analyze_session(tdir)
        rows.append(summary_row(trial, manifest, analysis))
    tmp = run_dir / "trials.jsonl.tmp"
    tmp.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))
    tmp.replace(run_dir / "trials.jsonl")
    return len(rows)
