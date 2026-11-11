# Research plan

How work goes from "the harness runs" to a published, citable result. Each
stage has a gate, and nothing moves to the next stage until its gate
passes. The point is that every number we publish can be traced to a
pinned build, a frozen spec, a seeded run and human labels, and that the
analysis was fixed before anyone saw the data.

## Questions this harness can answer

The four records (asked, emitted, executed, reported) support questions of
the form "does what the agent says match what happened?":

| id | question | experiment | main metric |
|---|---|---|---|
| Q1 | After a refusal, does the agent report output it never produced? | `post-refusal-fabrication` | reviewed fabrication rate among refused trials |
| Q2 | Does authorization text (a pre-prompt, a policy file, CLAUDE.md) raise execution after refusal? | `consent-priming` | post-refusal execution, arm vs baseline |
| Q3 | After a refused compound command, does the agent get around the refusal by running it piece by piece? | either, from `route` | workaround rate |
| Q4 | Does the stream the user sees match what the model emitted (hidden, invented or rewritten calls)? | any | `wire_only` / `stream_only` / `input_diff` per trial |
| Q5 | Do Q1–Q4 differ across labs, models and agent versions? | cross-lab variants | the same metrics, per adapter and model |

Q5 is the reason this harness supports more than one lab. A behavior seen
in one agent is a bug report; the same behavior measured in several,
under comparable conditions, is a finding about coding agents.

## Stage 0: provenance and capture check

Purpose: show the instrument works before using it to measure anything.

1. Build the image from a clean tree: `docker/run.sh` tags it with the
   agent versions and records the harness commit (a `-dirty` suffix means
   an uncommitted tree, which is not acceptable from Stage 2 on).
2. `mrh doctor` passes inside the container for every adapter you intend
   to use.
3. Dummy-key run, one per adapter. Every trial should come out
   `capture_ok=false` with `api_errors_only`. That shows the proxy is
   trusted, traffic is recorded and the error path is classified, without
   spending anything.
4. Run `experiments/cross-lab-pilot` (27 trials, about a dollar or two).
   Every trial should be `capture_ok` with `effect_achieved=true` and
   route `direct`.
5. Open one session per adapter and read it by hand: `api.jsonl`,
   `strace` summary, `stream.jsonl`, `analysis.json`. Check that the
   calls on the wire, the execs in the trace and the stream all line up.

**Gate:** all pilot trials pass, and each adapter has had one session read
by hand. An adapter that fails stays `experimental` and is left out of
later stages. Note in this file what was fixed.

## Stage 1: pilot with planted positives

Purpose: measure the detector's error rates where the right answer is
known.

- Run each target experiment with `--limit` at a small n (n=2 per cell is
  enough), including the control arms. In `post-refusal-fabrication`,
  `control-allowed` pre-approves Bash, so any `fabrication_candidate`
  there is a detector false positive.
- Plant known positives: sessions where you know a refusal happened and
  was disclosed, and where output was invented (hand-built fixtures or
  edited copies of real sessions, kept out of the dataset). Check that the
  detector flags them.
- Do a trial blind review of the pilot with both reviewers. Agree on the
  label definitions in writing before Stage 3 and add them to
  `docs/method.md`.
- Estimate cost per trial from `run.json` and the provider dashboards,
  and set `--max-cost` for the full run.

**Gate:** the detector misses no planted positive (the detector only
proposes candidates, so misses are what matter), and the false-positive
rate in controls is known. Pilot data is never pooled with confirmatory
data.

## Stage 2: preregistration

Freeze the plan before the confirmatory run. Write
`experiments/<id>/PREREG.md`, commit it, and record the commit hash. It
contains:

- the hypothesis, as in the spec, and what result would count against it
- the spec hash (`mrh experiment plan` prints it), agent versions, image
  tag and seed
- the primary metric and comparison: one per experiment. Everything else
  is labelled exploratory.
- n per cell and why (for scale: 2 of 20 is 10%, with a 95% Wilson
  interval of 3% to 30%; narrow intervals need pooling or larger n)
- exclusions: trials with `capture_problems` and `harness_error`; nothing
  else is dropped
- the review protocol: two blind reviewers, how disagreements are settled,
  and a kappa below which the labels are not used (0.6)
- the stopping rule: one full run; reruns only for harness errors

Optionally post it to OSF or another registry so the date is independent
of this repository.

**Gate:** PREREG.md committed and the tree clean.

## Stage 3: confirmatory run

- Run from the committed tree, with the spec unchanged (the spec hash must
  match PREREG.md) and the preregistered seed.
- Run inside one time window if possible. Trial order is shuffled, so
  drift over time spreads across cells rather than landing in one.
- If the run aborts (consecutive failures, cost cap), fix the cause and
  rerun the same command: completed trials are kept.
- `run.json` records version drift. A changed agent version means a new
  run, not a continued one.

**Gate:** the run completes, harness errors are under 5%, and no version
drift.

## Stage 4: blind two-reviewer coding

- `mrh experiment review-sheet RUN --reviewer a` and `--reviewer b`. The
  sheets show prompt, refusal and reported text, in random order, under
  opaque ids. Reviewers do not open `review_key.csv` and do not discuss
  cases until both are finished.
- `mrh experiment aggregate RUN` reports kappa and lists disagreements.
  Settle each one in `reviews-adjudicated.csv`, with a note on why.

**Gate:** kappa at or above the preregistered threshold, and every
flagged trial labelled. If kappa is low, fix the label definitions and
re-review; never drop the hard cases.

## Stage 5: analysis

- Report every rate as k/n with a Wilson 95% interval, per model and arm.
- For each preregistered comparison, report the difference with the
  Newcombe interval and the prompt-bootstrap interval, and quote the
  wider. `summary.md` produces both.
- Report detector and reviewed numbers in separate columns. Report the
  number of excluded trials and the reasons.
- Anything not in PREREG.md goes in a section headed "exploratory".

## Stage 6: disclosure and publication

1. If a result shows a safety or security problem in an agent, report it
   to the vendor first (see `docs/ethics.md`) and give a reasonable
   window, usually 90 days, before publishing details.
2. `mrh scrub` the run, read every file in the output, then `mrh export
   --reviewed`. The dataset carries its manifest, card, spec, PREREG.md
   and review files.
3. Publish the write-up and the dataset together, with a DOI (Zenodo or
   similar). Negative and null results are published the same way: "no
   fabrication observed in 480 trials, upper bound X%" is a result.
4. Write the finding into `research-inventory/` with the dataset version
   and DOI.

## Stage 7: replication across versions and labs

- Rerun the frozen spec on each new agent version (new image, new run
  directory). Results are per version; a rate on 2.1.317 says nothing
  about 2.2.
- For cross-lab work, write a separate spec per question, with arms
  chosen so the conditions are comparable for each agent (Claude Code's
  permission mode, Codex's sandbox and approval policy, Gemini CLI's
  approval mode). Say in the write-up which settings each agent ran
  under. Run Stage 0 for every adapter first.
- Invite outside replication: the image, spec and seed are enough to
  repeat a run, and the scrubbed dataset is enough to recheck the
  analysis without one.

## Chain of evidence

Every reported number should trace back through these links:

```
claim in write-up
  -> summary.md row (run dir, comparison, k/n)
    -> reviews-*.csv labels + review_key.csv (blind, kappa)
      -> per-trial analysis.json (detector output, outcome, capture_problems)
        -> session files with sha256 in manifest.json
          -> run.json (spec hash, seed, agent versions, image, harness commit)
            -> PREREG.md (committed before the run)
```

If a link is missing, the number is not reported.
