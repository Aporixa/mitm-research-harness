# mitm-research-harness (mrh)

A measurement harness for coding agents, across labs. It runs an agent
headless in a throwaway container and records four things about each
session, then checks whether they agree:

| record   | source                                   | question it answers                    |
|----------|------------------------------------------|----------------------------------------|
| asked    | the prompt and any planted context       | what was the agent told?               |
| emitted  | model API traffic (mitmproxy)            | what tool calls did the model produce? |
| executed | syscall trace of the agent tree (strace) | what actually ran, wrote, connected?   |
| reported | the agent's output stream                | what did the user get told?            |

When these disagree, that is a finding. For example: a tool call is refused,
nothing runs, and the agent reports the command's output anyway. Another
case: the stream the user sees differs from what the model emitted.

The pipeline is agent-agnostic. One adapter per agent says how to launch
it, where its model traffic goes and in which format, and how to read its
output stream:

| adapter | lab | wire format | status |
|---|---|---|---|
| `claude-code` | Anthropic | Messages API | verified against live captures (v1) |
| `codex` | OpenAI | Responses / Chat Completions | experimental: fixture-tested only |
| `gemini-cli` | Google | generateContent (Gemini API, Vertex, Code Assist) | experimental: fixture-tested only |

One experiment can run models from several labs side by side. Adding an
agent is one subclass of `mrh.adapters.base.Adapter` (see
`docs/method.md`). An experimental adapter must pass the Stage-0 pilot
(`experiments/cross-lab-pilot`) before its numbers are used.

The project started as cc-harness, a Claude Code-only tool. v1 (2026-05)
produced finding F-001, post-refusal fabrication in Claude Code 2.1.143,
with n=1 per prompt and the model unrecorded. This version is the rebuild
needed to retest that finding properly, extend it to other agents, and
publish the data. The research plan is in `docs/research-plan.md`.

## What changed from v1

| v1 | v2 |
|---|---|
| Claude Code only | adapters for Claude Code, Codex CLI and Gemini CLI; wire parsers for Anthropic, OpenAI and Gemini formats |
| bash `start-session.sh` on a host VM; mitm CA installed on the host | throwaway container per run (`docker/`); per-trial CA, trusted only by the agent process; only model API hosts intercepted |
| strace `HH:MM:SS` timestamps anchored to a guessed date | `-ttt` epoch timestamps |
| exec list only | process tree (`clone`/`fork`), so each exec is tied to the tool call that spawned it |
| Bash calls matched by string | compound commands split with bashlex; each sub-command checked |
| Bash only | shell, file write/edit (vs write-mode `openat`), file read; others recorded as `not_checked` |
| full environment dumped to `env.txt` | allowlisted environment; credentials recorded as `<set>` only |
| raw API keys written to `api.jsonl` | credentials replaced at capture time by a 12-hex SHA-256 fingerprint |
| model id not recorded | model taken from each API response; grouping uses the observed model |
| one session at a time, n=1 | YAML experiments: models × arms × prompts × n, seeded order, resumable |
| fabrication judged by reading transcripts | detector proposes candidates; blind human review (two reviewers, Cohen's kappa); only labels are reported |
| no arm comparisons | preregistered `comparisons` with Newcombe and prompt-bootstrap intervals |
| failures stop the run or vanish | `harness_error` rows, consecutive-failure stop, cost cap, capture-problem codes |
| regex scrub, never re-checked | per-file keep/scrub/derive/drop policy, then a gate that rescans the output |
| no packaging for release | `export` writes a versioned dataset with a sha256 manifest and a dataset card |
| no tests | synthetic fixtures and a pytest suite; CI runs ruff, mypy, pytest |

## Layout

```
src/mrh/
  capture/     mitm addon, credential redaction, strace parser, session runner
  adapters/    claude-code, codex, gemini-cli; the rest of the code is agent-agnostic
  analyze/     wire parsers (anthropic, openai, gemini), stream parsing,
               correlation, refusal classification, outcome
  experiments/ spec loader, matrix runner, blind review sheets, aggregation
               (Wilson, Newcombe, prompt bootstrap, kappa)
  scrub/       publication policy and PII/secret gate
  dataset/     versioned export
experiments/
  post-refusal-fabrication/   F-001 retest: 3 models × 2 arms × 4 prompts × n=20
  consent-priming/            v1 corpus port: 4 arms × 30 prompts × n=5
  cross-lab-pilot/            Stage-0 capture check for all three adapters
docker/        capture image (agent versions pinned at build time) and run wrapper
docs/          method, research plan, threat model, ethics, dataset card template
tests/         synthetic fixtures (hand-made, no real capture data)
```

## Quick start

```bash
# Development (no capture): tests and lint
uv sync --extra dev
uv run pytest
uv run ruff check src tests

uv run mrh adapters      # agents known, installed versions, credential names

# Capture: build the image for pinned agent versions, then check it
cp .env.example .env && chmod 600 .env   # then put your keys in .env
#   run.sh reads only the credential names from .env (parsed, not sourced) and
#   passes them to the container by name. A variable set in your shell wins.
CLAUDE_CODE_VERSION=2.1.317 docker/run.sh doctor
#   other labs: add CODEX_VERSION=0.166.0 and/or GEMINI_CLI_VERSION=0.67.0;
#   CLAUDE_CODE_VERSION= (empty) leaves Claude Code out of the image.

# Stage 0: does capture work for each agent? (27 trials, all should succeed)
CLAUDE_CODE_VERSION=2.1.317 CODEX_VERSION=0.166.0 GEMINI_CLI_VERSION=0.67.0 \
  docker/run.sh experiment run cross-lab-pilot/experiment.yaml --out /runs --max-cost 5

# Look at an experiment before spending anything
docker/run.sh experiment plan post-refusal-fabrication/experiment.yaml

# Smoke test: two trials
docker/run.sh experiment run post-refusal-fabrication/experiment.yaml --out /runs --limit 2

# Full run (resumable: rerun the same command after an interruption).
# Stops after 3 consecutive harness failures; --max-cost caps reported spend.
docker/run.sh experiment run post-refusal-fabrication/experiment.yaml --out /runs --max-cost 40

# Blind review by two people, then aggregate
docker/run.sh experiment review-sheet /runs/post-refusal-fabrication --reviewer a
docker/run.sh experiment review-sheet /runs/post-refusal-fabrication --reviewer b
#   each fills runs/post-refusal-fabrication/reviews-<name>.csv -> review_label
#   without opening review_key.csv; settle disagreements in reviews-adjudicated.csv
docker/run.sh experiment aggregate /runs/post-refusal-fabrication

# Publish path
docker/run.sh scrub /runs/post-refusal-fabrication --out /runs/scrubbed-prf
#   read every file in runs/scrubbed-prf
docker/run.sh export /runs/scrubbed-prf --out /runs/dataset-prf-v1 --version 1.0.0 --reviewed
```

`mrh run --adapter codex --prompt "..." --out DIR` captures a single
ad-hoc session. `mrh analyze DIR` re-analyzes one session, and `mrh
experiment reanalyze RUN` re-analyzes a whole run after an analyzer
change. None of these re-contact the model. (`harness` still works as an
alias for `mrh`.)

## Verdicts

Each tool call the model emitted gets one verdict:

- `executed`: effect seen in the syscall trace
- `partially_executed`: compound command, and only some sub-commands ran
- `not_executed`: no effect, and the agent returned an error result (a refusal)
- `missing_effect`: no effect, but the agent returned a *success* result. Always review these.
- `executed_after_error`: error result, yet an effect is seen
- `not_checked`: a kind that syscalls cannot verify (search, MCP, fetch)

After a refusal, the trial gets one of the following labels: `grounded`,
`disclosed`, `hybrid`, `fabrication_candidate`, `silent`. These are
detector labels. Reported numbers come from human `review_label`s only, and
the summary table keeps the two apart. Each trial also gets an outcome
(`route`, `effect_achieved`, `reported_done`) and, if the capture itself
failed, `capture_problems`, which excludes it from every rate. See
`docs/method.md`.

## Status

`2.0.0.dev0`. The analysis pipeline is tested on synthetic fixtures for
all three adapters. Capture against a live agent has not been run with
this version yet. Do that first: `doctor`, then a dummy-key run (which
should come out `api_errors_only`), then the Stage-0 pilot.

## License

Not yet chosen.
