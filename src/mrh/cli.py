"""mrh: mitm-research-harness command line (`harness` is an alias).

    mrh doctor
    mrh adapters
    mrh run --prompt "..." --out runs/adhoc/s1 [--adapter claude-code|codex|gemini-cli] [--model M]
    mrh analyze <session-dir> [--expected-output TEXT | --expected-pattern REGEX]
    mrh experiment plan <experiment.yaml>
    mrh experiment run <experiment.yaml> --out runs/ [--limit N] [--only SUBSTR]
                       [--max-cost USD] [--max-consecutive-failures N]
    mrh experiment reanalyze <run-dir>
    mrh experiment review-sheet <run-dir> [--all] [--reviewer NAME]
    mrh experiment aggregate <run-dir>
    mrh scrub <run-dir> --out <dir> [--term WORD ...] [--user U --host H]
    mrh export <scrubbed-dir> --out <dataset-dir> --version V --reviewed
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def cmd_doctor(_: argparse.Namespace) -> int:
    from mrh.adapters import ADAPTERS

    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"  [{'ok' if good else '!!'}] {label}{(': ' + detail) if detail else ''}")

    from mrh.capture.strace import STRACE_ARGS

    print("mrh doctor")
    check("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    for tool in ("strace", "mitmdump"):
        check(tool, shutil.which(tool) is not None, shutil.which(tool) or "not on PATH")
    if shutil.which("strace"):
        # Same flags as a capture, so an strace too old for them fails here.
        r = subprocess.run(["strace", *STRACE_ARGS, "-o", "/dev/null", "--", "true"],
                           capture_output=True, text=True)
        check("strace with capture flags", r.returncode == 0,
              "" if r.returncode == 0 else (r.stderr.strip().splitlines() or ["failed"])[-1]
              + " (ptrace needs --cap-add SYS_PTRACE)")
    try:
        import bashlex  # noqa: F401
        check("bashlex", True)
    except ImportError:
        check("bashlex", False, "pip install bashlex")
    installed = 0
    for name, cls in ADAPTERS.items():
        a = cls()
        v = a.version()
        if v == "not-installed":
            print(f"  [--] adapter {name} ({a.provider}): not installed (skipped)")
            continue
        installed += 1
        tag = " [experimental]" if a.experimental else ""
        check(f"adapter {name} ({a.provider}){tag}", True, v)
        present = [n for n in a.credential_env if os.environ.get(n)]
        check(f"{name} credential", bool(present),
              ", ".join(present) + " set (values not shown)" if present else
              "none of " + ", ".join(a.credential_env))
    check("at least one agent installed", installed > 0)
    check("in container", Path("/.dockerenv").exists(),
          "" if Path("/.dockerenv").exists() else "captures belong in the container (docker/)")
    return 0 if ok else 1


def cmd_adapters(_: argparse.Namespace) -> int:
    from mrh.adapters import ADAPTERS

    for name, cls in ADAPTERS.items():
        a = cls()
        status = "experimental" if a.experimental else "verified"
        print(f"{name:12} {a.provider:10} {a.wire_format:20} {status:13} "
              f"{a.version():16} hosts: {', '.join(a.wire_hosts)}")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    from mrh.adapters import get_adapter
    from mrh.analyze.session import analyze_session
    from mrh.capture.session import run_session

    text = a.prompt if a.prompt else Path(a.prompt_file).read_text()
    trial = {"trial_id": Path(a.out).name, "experiment": "adhoc", "model": a.model,
             "arm": {"id": "adhoc", "permission_mode": a.permission_mode, "sandbox": a.sandbox},
             "prompt": {"id": "adhoc", "text": text}, "expected_output": a.expected_output,
             "expected_pattern": a.expected_pattern, "adapter": a.adapter, "rep": 1}
    adapter = get_adapter(a.adapter)
    run_session(adapter, trial, Path(a.out), timeout_s=a.timeout, use_strace=not a.no_strace)
    analysis = analyze_session(Path(a.out), a.expected_output, a.expected_pattern)
    _print_analysis(analysis)
    return 0


def _print_analysis(an: dict[str, Any]) -> None:
    print(f"model: {an.get('model')}  capture_ok: {an.get('capture_ok')}")
    if an.get("capture_problems"):
        print(f"capture problems: {', '.join(an['capture_problems'])}")
    out = an.get("outcome") or {}
    print(f"route: {out.get('route')}  effect_achieved: {out.get('effect_achieved')}  "
          f"reported_done: {out.get('reported_done')}")
    print(f"verdicts: {an.get('verdict_counts')}")
    ref = an.get("refusal") or {}
    print(f"refusal: {ref.get('label')}  needs_review: {ref.get('needs_review')}")
    c = an["correlation"]
    if c["wire_only"] or c["stream_only"] or c["input_diff"]:
        print(f"wire/stream mismatch: wire_only={len(c['wire_only'])} "
              f"stream_only={len(c['stream_only'])} input_diff={len(c['input_diff'])}")


def cmd_analyze(a: argparse.Namespace) -> int:
    from mrh.analyze.session import analyze_session

    _print_analysis(analyze_session(Path(a.session), a.expected_output, a.expected_pattern))
    return 0


def cmd_plan(a: argparse.Namespace) -> int:
    from mrh.experiments.spec import load

    exp = load(Path(a.spec))
    trials = exp.trials()
    print(f"{exp.id}  spec {exp.spec_hash()}  adapters "
          + ", ".join(f"{n} {exp.pinned_version(n) or '(unpinned)'}" for n in exp.adapters()))
    print(f"hypothesis: {exp.hypothesis}")
    print(f"{len(exp.models)} models x {len(exp.arms)} arms x {len(exp.prompts)} prompts "
          f"x n={exp.n} = {len(trials)} trials")
    per_arm = Counter(t["arm"]["id"] for t in trials)
    print("arms: " + ", ".join(f"{k}={v}" for k, v in per_arm.items()))
    if exp.comparisons:
        print("comparisons: " + ", ".join(f"{a} vs {b}" for a, b in exp.comparisons))
    for t in trials[: a.show]:
        print("  " + t["trial_id"])
    return 0


def cmd_exp_run(a: argparse.Namespace) -> int:
    from mrh.experiments.runner import run
    from mrh.experiments.spec import load

    exp = load(Path(a.spec))
    run_dir = run(exp, Path(a.spec), Path(a.out), limit=a.limit,
                  allow_version_drift=a.allow_version_drift, only=a.only,
                  max_consecutive_failures=a.max_consecutive_failures, max_cost_usd=a.max_cost)
    print(run_dir)
    return 0


def cmd_reanalyze(a: argparse.Namespace) -> int:
    from mrh.experiments.runner import reanalyze

    print(f"reanalyzed {reanalyze(Path(a.run_dir))} trials")
    return 0


def cmd_review_sheet(a: argparse.Namespace) -> int:
    from mrh.experiments.aggregate import REVIEW_LABELS, write_review_sheet

    path = write_review_sheet(Path(a.run_dir), include_all=a.all, reviewer=a.reviewer)
    print(f"{path}\nfill review_label with one of: {', '.join(REVIEW_LABELS)}\n"
          "Do not open review_key.csv until review is finished; it unblinds model and arm.")
    return 0


def cmd_aggregate(a: argparse.Namespace) -> int:
    from mrh.experiments.aggregate import aggregate

    agg = aggregate(Path(a.run_dir))
    print((Path(a.run_dir) / "summary.md").read_text())
    print(json.dumps({"trials": agg["trials"], "reviews": agg["reviews"],
                      "review_kappa": agg["review_kappa"]}))
    return 0


def cmd_scrub(a: argparse.Namespace) -> int:
    from mrh.scrub import Identity, gate, scrub_run

    ident = Identity.current()
    # A run captured in the container has the container's user and host; the
    # operator's own names may still appear (bind mounts, prompts).
    if a.user:
        ident.user = a.user
    if a.host:
        ident.host = a.host
    report = scrub_run(Path(a.run_dir), Path(a.out), ident=ident, extra_terms=a.term or [])
    for k, v in report.items():
        print(f"{k}: {len(v)} files")
    hits = gate(Path(a.out), ident=ident, extra_terms=a.term or [])
    if hits:
        print(f"GATE FAILED: {len(hits)} hits", file=sys.stderr)
        for h in hits[:20]:
            print(f"  {h}", file=sys.stderr)
        return 2
    print("gate: pass. Now read every file in the output before exporting.")
    return 0


def cmd_export(a: argparse.Namespace) -> int:
    from mrh.dataset import export

    m = export(Path(a.scrubbed), Path(a.out), a.version, a.reviewed, extra_terms=a.term or [])
    print(f"exported {len(m['files'])} files to {a.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mrh", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("adapters", help="list agent adapters").set_defaults(fn=cmd_adapters)

    r = sub.add_parser("run", help="one captured session")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--prompt")
    g.add_argument("--prompt-file")
    r.add_argument("--out", required=True)
    r.add_argument("--adapter", default="claude-code", help="see `mrh adapters`")
    r.add_argument("--model")
    r.add_argument("--permission-mode",
                   help="claude-code permission mode / gemini-cli approval mode")
    r.add_argument("--sandbox", help="codex sandbox (read-only | workspace-write | danger-full-access)")
    r.add_argument("--expected-output")
    r.add_argument("--expected-pattern", help="regex the true output must match")
    r.add_argument("--timeout", type=int, default=300)
    r.add_argument("--no-strace", action="store_true")
    r.set_defaults(fn=cmd_run)

    an = sub.add_parser("analyze")
    an.add_argument("session")
    an.add_argument("--expected-output")
    an.add_argument("--expected-pattern")
    an.set_defaults(fn=cmd_analyze)

    e = sub.add_parser("experiment").add_subparsers(dest="ecmd", required=True)
    ep = e.add_parser("plan")
    ep.add_argument("spec")
    ep.add_argument("--show", type=int, default=5)
    ep.set_defaults(fn=cmd_plan)
    er = e.add_parser("run")
    er.add_argument("spec")
    er.add_argument("--out", required=True)
    er.add_argument("--limit", type=int)
    er.add_argument("--only")
    er.add_argument("--allow-version-drift", action="store_true")
    er.add_argument("--max-cost", type=float, help="stop at this agent-reported USD total")
    er.add_argument("--max-consecutive-failures", type=int, default=3,
                    help="stop after N failed trials in a row (0 disables)")
    er.set_defaults(fn=cmd_exp_run)
    ea = e.add_parser("reanalyze")
    ea.add_argument("run_dir")
    ea.set_defaults(fn=cmd_reanalyze)
    ev = e.add_parser("review-sheet")
    ev.add_argument("run_dir")
    ev.add_argument("--all", action="store_true")
    ev.add_argument("--reviewer", help="writes reviews-<name>.csv (two reviewers -> kappa)")
    ev.set_defaults(fn=cmd_review_sheet)
    eg = e.add_parser("aggregate")
    eg.add_argument("run_dir")
    eg.set_defaults(fn=cmd_aggregate)

    s = sub.add_parser("scrub")
    s.add_argument("run_dir")
    s.add_argument("--out", required=True)
    s.add_argument("--term", action="append", help="extra string to redact (repeatable)")
    s.add_argument("--user", help="user name to redact (default: current user)")
    s.add_argument("--host", help="host name to redact (default: current host)")
    s.set_defaults(fn=cmd_scrub)

    x = sub.add_parser("export")
    x.add_argument("scrubbed")
    x.add_argument("--out", required=True)
    x.add_argument("--version", required=True)
    x.add_argument("--reviewed", action="store_true")
    x.add_argument("--term", action="append")
    x.set_defaults(fn=cmd_export)

    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
