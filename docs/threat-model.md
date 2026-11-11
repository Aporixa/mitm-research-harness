# Threat model (for the harness itself)

The harness runs untrusted model output as shell commands, and stores full
conversations. What it protects, and how:

| asset | risk | control |
|---|---|---|
| operator host | agent runs arbitrary commands | capture only in the container; unprivileged user; `no-new-privileges`; memory/pid limits; `doctor` warns outside a container |
| host trust store | MITM CA trusted system-wide (v1) | CA per trial, in a temp dir deleted after the trial; passed only to the agent process via env (`NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `CODEX_CA_CERTIFICATE`); only model API hosts are intercepted |
| API credential | written to disk in headers or env dumps | redacted at capture (fingerprint only); every run file scrubbed of the key value and of keys echoed in provider errors; env recorded by name; passed to docker by name |
| operator identity | username, hostname, home path in traces | raw traces never leave `runs/` (gitignored); scrub replaces identity terms; gate rescans |
| vendor system prompt / conversation | republished verbatim | raw `wire.jsonl` is dropped by the scrub policy; only `wire.calls.jsonl` (tool calls + model) is kept |
| undisclosed vulnerabilities | publishing before the vendor has fixed it | export requires `--reviewed`; see ethics.md |

Not covered: the container has general outbound network access (the agent
needs the API). An experiment whose prompts reach out to third parties
needs a network policy; the shipped experiments resolve `example.com` and
read container-local files only.
