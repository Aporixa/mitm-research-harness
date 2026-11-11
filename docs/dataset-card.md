# Dataset card: {{experiment}} v{{version}}

## Summary

Agent sessions captured with mitm-research-harness (mrh). Each trial records the prompt,
the model's tool calls (from API traffic), the per-call execution verdicts
(from a syscall trace), and the agent's output stream.

- Agents and versions: {{agent_version}}
- Models observed: {{models}}
- Trials: {{trials}} (human-reviewed: {{reviewed}})
- Exported: {{exported}}
- Harness commit: {{commit}}

## Contents

`data/` holds the scrubbed run. Raw API traffic, syscall traces and agent
stderr are **not** included: they contain the vendor's system prompt,
host paths and identities. Each trial keeps `analysis.json`, `stream.jsonl`
(session ids and cwd removed), `wire.calls.jsonl`, a reduced
`manifest.json` and its `context/` (prompt and planted files).
`MANIFEST.json` lists the sha256 of every file.

## Intended use

Measuring agreement between what coding agents are asked, emit, execute
and report. Not for training models to evade detection of the behaviors
measured here.

## Labels

`refusal.label` in each analysis is an automatic candidate. Human labels are
in `reviews*.csv`, keyed by an opaque `review_id`; `review_key.csv` maps the
ids to trials. Reviewers were blind to model and arm. Quote rates from the
human labels only.

## Limits

See `docs/method.md` in the harness repository. Results apply to the
versions above, during the capture window.
