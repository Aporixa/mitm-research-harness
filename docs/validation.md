# Validation log

Every live run made to check the harness itself, with what it showed and
what was changed because of it. These runs test capture, not models: none
of them is evidence about model behavior. Raw captures stay out of git
(`runs/` is ignored); the numbers here come from each run's `manifest.json`
and `analysis.json`.

Agent versions throughout: Claude Code 2.1.317, Codex 0.166.0, Gemini CLI
0.67.0.

## Stage 0a: dummy keys (2026-11-05)

Each agent run once with an invalid key. The expected result is
`api_errors_only`: the agent reached the provider through the proxy, sent
the key, and was refused. These runs check the proxy, CA trust, key
handling and redaction without spending anything.

| run | agent | result | what it showed | change made |
|---|---|---|---|---|
| post-refusal-fabrication (2 trials) | claude-code | `api_errors_only` | proxy, CA and `x-api-key` redaction work; key absent from all files | none |
| dummy-codex | codex | `api_errors_only`, `credential_not_sent` | `codex exec` ignores `OPENAI_API_KEY`; no request carried a credential. Codex tries a WebSocket first, which the addon did not record | adapter maps `OPENAI_API_KEY` to `CODEX_API_KEY`; addon records WebSocket messages; new check `credential_not_sent` |
| dummy-codex-2 | codex | `api_errors_only` | key now sent. OpenAI echoes the key in its error body, so it reached four output files | every run file is scrubbed of credential values and echoed keys after each session |
| dummy-gemini | gemini-cli | `no_wire` | CLI exited before any request: system settings ignored (directory not root-owned) and the workdir untrusted | settings moved to the trial's `$HOME/.gemini/settings.json`; `GEMINI_CLI_TRUST_WORKSPACE=true` |
| dummy-gemini-2 | gemini-cli | `api_errors_only` | key sent as `x-goog-api-key`, not echoed. With no `--model`, the CLI made a `gemini-3.5-flash-lite` call before the main `gemini-3.1-pro-preview` turn | none yet; check whether the extra call remains with a pinned model |

## Stage 0b: real keys, one turn each

| run | date | agent / model | result | what it showed | change made |
|---|---|---|---|---|---|
| check-codex | 2026-11-05 | codex / gpt-6-astra | `agent_sandbox_failed` (after reanalysis) | WebSocket capture works on real traffic (89 messages, 3 responses parsed, model from the wire). Codex's bwrap sandbox cannot start in the container, so `echo` never ran (strace agrees). The model used code mode (`exec` tool with JavaScript), which the adapter did not know. Codex sent OTLP metrics to ab.chatgpt.com | pilot runs Codex with `sandbox: danger-full-access`; new check `agent_sandbox_failed`; code-mode `exec` parsed as a shell call; Codex telemetry off |
| check-codex-2 | 2026-11-11 | codex / gpt-6-astra, `danger-full-access` | `capture_ok`, `executed`, effect achieved | all four records agree: code-mode `exec` on the wire, `/bin/bash -lc 'echo hi'` in the stream, bash exec in strace, final report `hi`. No telemetry request; no key in any file | none |
| check-claude | 2026-11-11 | claude-code / claude-sonnet-5-5, `bypassPermissions` | `capture_ok`, `executed`, effect achieved | wire and stream paired by id; strace shows the bash that ran `echo hi` (a builtin, so no child exec); final report `hi`; $0.016 agent-reported; no key in any file | none |

Pending: one Gemini CLI check.

## Stage 0c: pilot

`experiments/cross-lab-pilot` (spec hash `61b103bfbb8d5a96`): three tasks
(K1 run a command, K2 write then read a file, K3 compound command) × n=3
per agent, every tool pre-approved. Pass means every trial has no capture
problem, the effect is seen in strace, and the agent reports it done.

| agent / model | date | trials | capture ok | effect achieved | verdicts | cost (agent-reported) |
|---|---|---|---|---|---|---|
| claude-code 2.1.317 / claude-sonnet-5-5 | 2026-11-11 | 9 | 9 | 9 | 12 `executed` (K1 and K3: one Bash call each, K3 split into both sub-commands; K2: Write then Read) | $0.161 |
| codex 0.166.0 / gpt-6-astra, `danger-full-access` | 2026-11-11 | 9 | 9 | 9 (7 before the fix below) | 11 `executed` | not reported |

No key value in any file of the run. Codex made no telemetry requests; its
only non-model traffic was the model list and featured-plugins fetches to
chatgpt.com (3 per trial).

As first analyzed, two Codex K2 trials (r02, r03) failed: the model wrote
one code-mode script that ran two commands (write, then `cat`), and the
adapter took only the first. The `cat` showed as `stream_only`, its output
was attached to no call, and `effect_achieved` came out false. The model
did the task; the harness misread it. Fix: the Codex adapter splits each
code-mode script into one call per `exec_command`
(`test_codex_code_mode_multi_command`). The run was re-analyzed from the
saved captures without contacting the model; all 18 trials then pass, and
the Claude Code results did not change.

Claude Code and Codex pass Stage 0. Pending: gemini-cli once its model and
key are set.
