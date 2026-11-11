import csv
import json
import shutil
from pathlib import Path

import pytest

from mrh.analyze.session import analyze_session
from mrh.experiments.aggregate import (
    aggregate,
    cohen_kappa,
    newcombe,
    wilson,
    write_review_sheet,
)
from mrh.experiments.runner import summary_row
from mrh.experiments.spec import SpecError, load
from mrh.scrub import Identity, gate, scrub_run, scrub_text

ROOT = Path(__file__).parents[1]
FIX = Path(__file__).parent / "fixtures"


def test_both_shipped_specs_load_and_expand():
    pr = load(ROOT / "experiments/post-refusal-fabrication/experiment.yaml")
    assert len(pr.trials()) == 3 * 2 * 4 * 20
    cp = load(ROOT / "experiments/consent-priming/experiment.yaml")
    assert len(cp.prompts) == 30 and len(cp.arms) == 4
    d = next(a for a in cp.arms if a["id"] == "d-policy-claude-md")
    assert "CLAUDE.md" in d["plant_resolved"]
    assert {t["prompt"]["id"] for t in cp.trials()} == {f"p{i:02d}" for i in range(1, 31)}


def test_trial_order_is_deterministic():
    p = ROOT / "experiments/post-refusal-fabrication/experiment.yaml"
    assert [t["trial_id"] for t in load(p).trials()] == [t["trial_id"] for t in load(p).trials()]


def test_spec_rejects_path_escape(tmp_path):
    (tmp_path / "e.yaml").write_text(
        "id: x\nhypothesis: h\nmodels: [m]\narms: [{id: a, preprompt_file: ../../etc/passwd}]\n"
        "prompts: [{id: p, text: t}]\n")
    with pytest.raises(SpecError):
        load(tmp_path / "e.yaml")


def test_wilson_bounds():
    lo, hi = wilson(0, 20)
    assert lo == 0.0 and 0.15 < hi < 0.18
    lo, hi = wilson(10, 20)
    assert lo < 0.5 < hi


def _fake_run(tmp_path):
    run = tmp_path / "runs" / "exp"
    rows = []
    for i, name in enumerate(["refused", "executed", "refused"]):
        tid = f"m__default__F3__r{i + 1:02d}"
        tdir = run / "trials" / tid
        shutil.copytree(FIX / name, tdir)
        manifest = {"status": "exited", "agent_version": "2.1.317", "adapter": "claude-code",
                    "env": {"ANTHROPIC_API_KEY": "<set>", "HOME": "<per-trial>"},
                    "trial": {"expected_output": "alpha-bravo-charlie-9472"}}
        (tdir / "manifest.json").write_text(json.dumps(manifest))
        (tdir / "context").mkdir()
        (tdir / "context" / "prompt.txt").write_text("Run it")
        a = analyze_session(tdir)
        trial = {"trial_id": tid, "model": "m", "arm": {"id": "default"},
                 "prompt": {"id": "F3"}, "rep": i + 1}
        rows.append(summary_row(trial, manifest, a))
    (run / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (run / "run.json").write_text(json.dumps({"experiment": "exp", "spec_hash": "x"}))
    return run


def test_aggregate_separates_detector_from_review(tmp_path):
    run = _fake_run(tmp_path)
    agg = aggregate(run)
    cell = next(c for c in agg["cells"] if c["prompt"] == "*")
    assert cell["refusal"]["k"] == 2 and cell["refusal"]["n"] == 3
    assert cell["detector"]["fabrication_candidate"]["k"] == 2
    assert cell["reviewed_n"] == 0 and cell["flagged_unreviewed"] == 2

    sheet = write_review_sheet(run, seed=1)
    rows = list(csv.DictReader(sheet.open()))
    assert len(rows) == 2  # two flagged trials
    assert "trial_id" not in rows[0] and "model_observed" not in rows[0]  # blind
    rows[0]["review_label"] = "fabricated"
    rows[0]["review_note"] = "claimed hash it never computed"
    _write_csv(sheet, rows)
    agg = aggregate(run)
    cell = next(c for c in agg["cells"] if c["prompt"] == "*")
    assert cell["reviewed"]["fabricated"]["k"] == 1

    # Regenerating keeps the label, the note and the review id.
    write_review_sheet(run, seed=2)
    again = {r["review_id"]: r for r in csv.DictReader(sheet.open())}
    assert again[rows[0]["review_id"]]["review_label"] == "fabricated"
    assert again[rows[0]["review_id"]]["review_note"].startswith("claimed")


def _write_csv(path, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_two_reviewers_kappa_and_disagreement(tmp_path):
    run = _fake_run(tmp_path)
    a = write_review_sheet(run, reviewer="a", seed=1)
    b = write_review_sheet(run, reviewer="b", seed=1)
    ra, rb = list(csv.DictReader(a.open())), list(csv.DictReader(b.open()))
    for r in ra:
        r["review_label"] = "fabricated"
    for r in rb:
        r["review_label"] = "fabricated"
    rb[1]["review_label"] = "disclosed"
    _write_csv(a, ra)
    _write_csv(b, rb)
    agg = aggregate(run)
    assert agg["reviewers"] == ["a", "b"]
    assert agg["review_disagreements"] == 1 and agg["reviews"] == 1


def test_newcombe_and_kappa():
    d = newcombe(8, 10, 2, 10)
    assert d["diff"] == 0.6 and d["ci95"][0] > 0 and d["ci95"][1] <= 1
    assert newcombe(0, 0, 1, 2) is None
    assert cohen_kappa([("x", "x"), ("y", "y")]) == 1.0
    assert cohen_kappa([("x", "y"), ("y", "x")]) < 0


def test_arm_comparison(tmp_path):
    run = tmp_path / "exp"
    run.mkdir()
    rows = []
    for arm, hits in (("a", 1), ("b", 4)):
        for p in ("p1", "p2", "p3"):
            for i in range(5):
                rows.append({"trial_id": f"{arm}-{p}-{i}", "model_requested": "m",
                             "model_observed": "m", "arm": arm, "prompt": p, "capture_ok": True,
                             "refused_calls": 1 if i < hits else 0, "route": "direct",
                             "effect_achieved": i >= hits})
    (run / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    agg = aggregate(run, comparisons=[("b", "a")])
    c = agg["comparisons"][0]
    assert c["refusal"]["newcombe"]["diff"] == 0.6
    assert c["refusal"]["bootstrap_prompts"] is not None
    assert "Arm comparisons" in (run / "summary.md").read_text()


def test_scrub_policy_drops_raw_and_gate_passes(tmp_path):
    run = _fake_run(tmp_path)
    ident = Identity(user="zz-testuser", host="zz-testhost", home="/home/zz-testuser")
    (run / "trials.jsonl").write_text((run / "trials.jsonl").read_text()
                                      + '{"note": "zz-testhost /home/zz-testuser/x"}\n')
    out = tmp_path / "scrubbed"
    report = scrub_run(run, out, ident)
    dropped = set(report["drop"])
    assert any(p.endswith("strace.log") for p in dropped)
    assert any(p.endswith("/wire.jsonl") for p in dropped)
    assert not list(out.rglob("strace.log")) and not list(out.rglob("wire.jsonl"))
    assert list(out.rglob("wire.calls.jsonl"))
    m = json.loads(next(out.rglob("manifest.json")).read_text())
    assert m["env_names"] == ["ANTHROPIC_API_KEY", "HOME"] and "env" not in m
    assert gate(out, ident) == []


def test_scrub_text_and_gate_catch_secrets(tmp_path):
    ident = Identity(user="zz-u", host="zz-h", home="/home/zz-u")
    s = scrub_text("key sk-ant-abcdefghijklmnop mail a.b@corp.io ok@example.com", ident)
    assert "sk-ant" not in s and "corp.io" not in s and "ok@example.com" in s
    (tmp_path / "leak.txt").write_text("token ghp_" + "a" * 30)
    assert gate(tmp_path, ident)[0]["kind"] == "github_token"


def test_scrub_in_container_home_tmp_and_session_ids():
    # Regression: in the container HOME=/tmp. Trial dirs must not become ~/harness-trial-*,
    # and session UUIDs (argv, original_cwd) must not survive.
    ident = Identity(user="agent", host="abc123def456", home="/tmp")
    raw = ('{"original_cwd": "/tmp/harness-trial-6gy0a0p7/work", '
           '"argv": ["--session-id", "0fad9452-964c-4a84-a2a4-0fdeb0e0d3c0"]}')
    s = scrub_text(raw, ident)
    assert "harness-trial" not in s and "0fad9452" not in s and "/tmp/<trial>/work" in s


def test_gate_flags_structural_leaks(tmp_path):
    ident = Identity(user="zz-u", host="zz-h", home="/tmp")
    (tmp_path / "a.json").write_text('{"x": "~/harness-trial-abc"}')
    (tmp_path / "b.json").write_text('{"s": "0fad9452-964c-4a84-a2a4-0fdeb0e0d3c0"}')
    (tmp_path / "c.json").write_text('{"p": "/tmp/<trial>/work"}')
    kinds = {(h["file"], h["kind"]) for h in gate(tmp_path, ident)}
    assert kinds == {("a.json", "trial_dir"), ("b.json", "session_uuid")}
