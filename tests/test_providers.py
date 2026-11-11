"""Multi-provider pieces: model ids, OpenAI/Gemini wire formats, Codex and
Gemini CLI streams, pairing without shared ids, outcomes, capture checks,
multi-adapter specs and the runner's failure guard.

All inputs are synthetic and written inline; none is a real capture.
"""

import json
import re
from pathlib import Path

import pytest

from mrh.adapters import get_adapter
from mrh.analyze.correlate import CallVerdict, _pair, _workarounds
from mrh.analyze.refusal import contains_output, outcome
from mrh.analyze.session import capture_problems
from mrh.analyze.wire import PARSERS, WireParse
from mrh.capture.redact import redact_url
from mrh.capture.session import allow_hosts_regex
from mrh.events import ToolCall
from mrh.experiments.spec import SpecError, load
from mrh.models import normalize_model, same_model
from mrh.scrub import Identity, scrub_text


def _sse(events: list[dict]) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


def _flow(fid: str, url: str, req: dict, body: str, sse: bool = True, status: int = 200) -> list[dict]:
    ctype = "text/event-stream" if sse else "application/json"
    return [
        {"ts": 1.0, "direction": "request", "flow_id": fid, "url": url, "body": json.dumps(req)},
        {"ts": 2.0, "direction": "response", "flow_id": fid, "url": url, "status": status,
         "headers": {"content-type": ctype}, "body": body},
    ]


# ------------------------------------------------------------------ model ids

@pytest.mark.parametrize("raw,norm", [
    ("claude-sonnet-5-5-20261001", "claude-sonnet-5-5"),
    ("claude-opus-5-5[1m]", "claude-opus-5-5"),
    ("models/gemini-2.5-pro", "gemini-2.5-pro"),
    ("gpt-5-codex-2026-09-15", "gpt-5-codex"),
    ("Gemini-2.5-Flash-latest", "gemini-2.5-flash"),
    (None, None),
])
def test_normalize_model(raw, norm):
    assert normalize_model(raw) == norm


def test_same_model_ignores_snapshot_date():
    assert same_model("claude-haiku-5-5", "claude-haiku-5-5-20261001")
    assert not same_model("claude-haiku-5-5", "claude-sonnet-5-5")


# ----------------------------------------------------------------- OpenAI wire

def test_openai_responses_sse_tool_call():
    url = "https://api.openai.com/v1/responses"
    item = {"type": "function_call", "id": "fc_1", "call_id": "call_abc", "name": "shell",
            "arguments": ""}
    body = _sse([
        {"type": "response.created", "response": {"model": "gpt-test-2026-01-01"}},
        {"type": "response.output_item.added", "item": item},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1",
         "delta": '{"command": ["bash", "-lc", '},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '"ls"]}'},
        {"type": "response.output_item.done", "item": item},
        {"type": "response.completed",
         "response": {"model": "gpt-test-2026-01-01", "usage": {"output_tokens": 42}, "output": []}},
    ])
    w = PARSERS["openai"](_flow("f1", url, {"model": "gpt-test"}, body), lambda n: "shell")
    assert w.recognized == 1 and w.api_responses == 1
    [c] = w.tool_calls
    assert c.tool_id == "call_abc" and c.input == {"command": ["bash", "-lc", "ls"]}
    assert w.primary_model() == "gpt-test-2026-01-01"
    assert w.output_tokens["gpt-test-2026-01-01"] == 42


def test_openai_chat_completions_json_and_non_api_url():
    body = json.dumps({"object": "chat.completion", "model": "gpt-test", "usage": {
        "completion_tokens": 5}, "choices": [{"message": {"tool_calls": [{
            "id": "call_1", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}]}}]})
    recs = _flow("f1", "https://api.openai.com/v1/chat/completions", {}, body, sse=False)
    recs += _flow("f2", "https://api.openai.com/v1/models", {}, "{}", sse=False)
    w = PARSERS["openai"](recs, lambda n: "file_read")
    assert w.responses == 2 and w.api_responses == 1
    assert [c.input for c in w.tool_calls] == [{"path": "a.txt"}]


def test_openai_truncated_stream_marks_call():
    item = {"type": "function_call", "id": "fc_9", "call_id": "call_9", "name": "shell"}
    body = _sse([{"type": "response.created", "response": {"model": "m"}},
                 {"type": "response.output_item.added", "item": item},
                 {"type": "response.function_call_arguments.delta", "item_id": "fc_9",
                  "delta": '{"command": ["ls"'}])
    w = PARSERS["openai"](_flow("f", "https://api.openai.com/v1/responses", {}, body),
                          lambda n: "shell")
    assert w.tool_calls[0].truncated_stream


# ----------------------------------------------------------------- Gemini wire

def test_gemini_code_assist_envelope_and_synth_id():
    url = ("https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse")
    body = _sse([
        {"response": {"modelVersion": "gemini-test-001", "candidates": [{"content": {"parts": [
            {"text": "Running it."},
            {"functionCall": {"name": "run_shell_command", "args": {"command": "ls"}}}]}}]}},
        {"response": {"usageMetadata": {"candidatesTokenCount": 17}, "candidates": []}},
    ])
    w = PARSERS["gemini"](_flow("g1", url, {"model": "gemini-test"}, body), lambda n: "shell")
    [c] = w.tool_calls
    assert c.tool_id == "g1#0" and c.input == {"command": "ls"}
    assert c.model == "gemini-test-001"
    assert w.recognized == 1


def test_gemini_api_model_from_url_and_error_counted():
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
    ok = json.dumps({"candidates": [{"content": {"parts": [
        {"functionCall": {"id": "fc1", "name": "read_file", "args": {"absolute_path": "/x"}}}]}}]})
    recs = _flow("a", url, {}, ok, sse=False) + _flow("b", url, {}, '{"error": {}}', sse=False,
                                                     status=429)
    w = PARSERS["gemini"](recs, lambda n: "file_read")
    assert w.tool_calls[0].model == "gemini-test" and w.api_errors == 1


# --------------------------------------------------------------- agent streams

CODEX_STREAM = [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Checking."}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution",
                                        "command": "bash -lc 'ls -la'", "status": "completed",
                                        "exit_code": 0, "aggregated_output": "a.txt\n"}},
    {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "Done: a.txt"}},
    {"type": "item.completed", "item": {"id": "item_3", "type": "command_execution",
                                        "command": "rm -rf /", "status": "declined",
                                        "exit_code": None, "aggregated_output": ""}},
    {"type": "item.completed", "item": {"id": "item_4", "type": "agent_message",
                                        "text": "I won't run that."}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}},
]


def test_codex_stream():
    a = get_adapter("codex")
    sp = a.parse_stream(json.dumps(e) for e in CODEX_STREAM)
    assert [c.tool_id for c in sp.tool_calls] == ["item_1", "item_3"]
    r1, r3 = sp.tool_results
    assert not r1.is_error and r1.following_text == "Done: a.txt"
    assert r3.is_error and r3.following_text == "I won't run that."
    assert sp.final.text == "I won't run that." and not sp.final.is_error
    assert a.shell_command(sp.tool_calls[0]) == "ls -la"


def test_codex_patch_path():
    a = get_adapter("codex")
    c = ToolCall(tool_id="x", name="apply_patch", kind="file_edit", source="wire",
                 input={"input": "*** Begin Patch\n*** Add File: notes/a.md\n+hi\n*** End Patch"})
    assert a.file_path(c) == "notes/a.md"


def test_gemini_cli_stream():
    a = get_adapter("gemini-cli")
    events = [
        {"type": "init", "session_id": "s", "model": "gemini-test"},
        {"type": "message", "role": "user", "content": "list files"},
        {"type": "message", "role": "assistant", "content": "Listing.", "delta": True},
        {"type": "tool_use", "tool_name": "run_shell_command", "tool_id": "t1",
         "parameters": {"command": "ls"}},
        {"type": "tool_result", "tool_id": "t1", "status": "error",
         "error": {"message": "not approved"}},
        {"type": "message", "role": "assistant", "content": "It was blocked.", "delta": True},
        {"type": "result", "status": "success", "stats": {"total_tokens": 9}},
    ]
    sp = a.parse_stream(json.dumps(e) for e in events)
    assert sp.init["model"] == "gemini-test"
    [c] = sp.tool_calls
    assert c.kind == "shell" and a.shell_command(c) == "ls"
    [r] = sp.tool_results
    assert r.is_error and "not approved" in r.content and r.following_text == "It was blocked."
    assert sp.final.text == "It was blocked." and not sp.final.is_error


# --------------------------------------------------------------------- pairing

def test_pair_without_shared_ids_matches_argv_to_joined_command():
    a = get_adapter("codex")
    wire = [ToolCall("call_a", "shell", "shell", {"command": ["bash", "-lc", "cat x"]}, "wire"),
            ToolCall("call_b", "shell", "shell", {"command": ["bash", "-lc", "ls -la"]}, "wire")]
    stream = [ToolCall("item_1", "shell", "shell", {"command": "bash -lc 'ls -la'"}, "stream"),
              ToolCall("item_2", "shell", "shell", {"command": "bash -lc 'cat x'"}, "stream")]
    assert _pair(a, wire, stream) == {"call_a": "item_2", "call_b": "item_1"}


def test_pair_shared_ids_does_not_guess():
    a = get_adapter("claude-code")
    wire = [ToolCall("toolu_1", "Bash", "shell", {"command": "ls"}, "wire")]
    stream = [ToolCall("toolu_X", "Bash", "shell", {"command": "ls"}, "stream")]
    assert _pair(a, wire, stream) == {}


# ---------------------------------------------------------- outcome/workaround

def _v(tid, verdict, err=None, head=None, **detail):
    return CallVerdict(tool_id=tid, name="Bash", kind="shell", model="m", verdict=verdict,
                       detail=detail, result_is_error=err, result_head=head)


def test_workaround_detected():
    vs = [_v("1", "not_executed", True, command="cat a | sha256sum", operators=["|"],
             expected_execs=["cat", "sha256sum"]),
          _v("2", "executed", False, expected_execs=["cat"]),
          _v("3", "executed", False, expected_execs=["sha256sum"])]
    [w] = _workarounds(vs)
    assert w["refused_call"] == "1" and w["covered"] and w["ran_later"] == {"cat": "2", "sha256sum": "3"}


def test_outcome_routes_and_effect():
    calls = [{"verdict": "not_executed", "result_is_error": True},
             {"verdict": "executed", "result_is_error": False, "result_head": "abc123"}]
    o = outcome({"calls": calls}, "the answer is abc123", "abc123")
    assert o == {"route": "retry", "effect_achieved": True, "reported_done": True}
    o = outcome({"calls": calls[:1]}, "the answer is abc123", "abc123")
    assert o["route"] == "blocked" and o["effect_achieved"] is False and o["reported_done"]
    assert outcome({"calls": []}, None, None) == {
        "route": "no_action", "effect_achieved": None, "reported_done": None}


def test_expected_pattern():
    h = "a" * 64
    assert contains_output(f"hash: {h}", None, "[0-9a-f]{64}")
    assert not contains_output("hash: xyz", None, "[0-9a-f]{64}")


# --------------------------------------------------------------- capture checks

def test_capture_problems():
    w = WireParse()
    assert "no_wire" in capture_problems({"status": "exited"}, w, True, True, False)
    w = WireParse(responses=3, api_responses=2, api_errors=2)
    p = capture_problems({"status": "timeout"}, w, False, False, True)
    assert set(p) == {"status_timeout", "api_errors_only", "stream_fallback", "no_trace",
                      "no_final_report"}
    w = WireParse(responses=1, api_responses=1, recognized=1)
    assert capture_problems({"status": "exited"}, w, True, True, False) == []


def test_redact_url():
    u = redact_url("https://generativelanguage.googleapis.com/v1beta/x:generateContent?key=SECRET&alt=sse")
    assert "SECRET" not in u and "alt=sse" in u and "key=" in u


def test_allow_hosts_regex():
    rx = re.compile(allow_hosts_regex(("api.openai.com", "aiplatform.googleapis.com")))
    assert rx.match("api.openai.com:443")
    assert rx.match("us-central1-aiplatform.googleapis.com")
    assert not rx.match("api.openai.com.attacker.example")
    assert not rx.match("registry.npmjs.org:443")


def test_scrub_google_key_and_generic_user():
    ident = Identity(user="node", host="zz-host", home="/tmp")
    out = scrub_text("key AIza" + "B" * 35 + " via node on zz-host", ident)
    assert "AIza" not in out and "node" in out and "zz-host" not in out


# ---------------------------------------------------------------- specs/runner

def _spec(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "experiment.yaml"
    p.write_text(f"""
id: cross-lab
hypothesis: test
adapter: claude-code
agent_versions: {{claude-code: "1.0", codex: "0.1"}}
models:
  - claude-test
  - {{id: gpt-test, adapter: codex}}
arms: [{{id: a}}, {{id: b}}]
prompts: [{{id: p1, text: hi, expected_pattern: "[0-9]+"}}]
n: 2
{extra}
""")
    return p


def test_multi_adapter_spec(tmp_path):
    exp = load(_spec(tmp_path, "comparisons: [[b, a]]"))
    assert exp.adapters() == ["claude-code", "codex"]
    assert exp.pinned_version("codex") == "0.1"
    trials = exp.trials()
    assert len(trials) == 2 * 2 * 1 * 2
    assert {t["adapter"] for t in trials} == {"claude-code", "codex"}
    assert all(t["trial_id"].startswith(t["adapter"] + "--") for t in trials)
    assert trials[0]["expected_pattern"] == "[0-9]+"
    assert exp.comparisons == [("b", "a")]


def test_spec_rejects_unknown_arm_and_adapter(tmp_path):
    with pytest.raises(SpecError):
        load(_spec(tmp_path, "comparisons: [[b, zzz]]"))
    p = _spec(tmp_path)
    p.write_text(p.read_text().replace("adapter: codex", "adapter: nope"))
    with pytest.raises(SpecError):
        load(p)


def test_runner_stops_after_consecutive_failures(tmp_path, monkeypatch):
    from mrh.experiments import runner

    class Dummy:
        experimental = False

        def version(self):
            return "1.0" if self is dummies["claude-code"] else "0.1"

    dummies = {"claude-code": Dummy(), "codex": Dummy()}
    monkeypatch.setattr(runner, "get_adapter", lambda name: dummies[name])

    def boom(*a, **k):
        raise RuntimeError("proxy not trusted")

    monkeypatch.setattr(runner, "run_session", boom)
    spec = _spec(tmp_path)
    with pytest.raises(runner.RunAborted):
        runner.run(load(spec), spec, tmp_path / "runs", max_consecutive_failures=3)
    rows = [json.loads(x) for x in (tmp_path / "runs" / "cross-lab" / "trials.jsonl").read_text().splitlines()]
    assert len(rows) == 3 and all(r["status"] == "harness_error" for r in rows)
    meta = json.loads((tmp_path / "runs" / "cross-lab" / "run.json").read_text())
    assert meta["agent_versions_found"] == {"claude-code": "1.0", "codex": "0.1"}


# --------------------------------------------------- first dummy-key captures


def _ws(fid, ts, body, client=False, url="https://api.openai.com/v1/responses"):
    import json as _j
    return {"direction": "ws_message", "flow_id": fid, "ts": ts, "url": url,
            "from_client": client, "body": body if isinstance(body, str) else _j.dumps(body)}


def test_openai_websocket_responses():
    from mrh.adapters import get_adapter
    from mrh.analyze.wire import PARSERS

    kind = get_adapter("codex").tool_kind
    url = "https://api.openai.com/v1/responses"
    call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "shell",
            "arguments": '{"command": ["bash", "-lc", "echo hi"]}'}
    recs = [
        {"direction": "request", "flow_id": "w", "method": "GET", "url": url,
         "headers": {"Authorization": "<redacted:sha256:abc>", "Upgrade": "websocket"}},
        {"direction": "response", "flow_id": "w", "status": 101, "url": url, "headers": {}},
        _ws("w", 1.0, {"type": "response.create", "model": "gpt-5-codex"}, client=True),
        _ws("w", 1.1, {"type": "response.created", "response": {"model": "gpt-5-codex"}}),
        _ws("w", 1.2, {"type": "response.output_item.added", "item": call}),
        _ws("w", 1.3, {"type": "response.output_item.done", "item": call}),
        _ws("w", 1.4, {"type": "response.completed",
                       "response": {"model": "gpt-5-codex", "usage": {"output_tokens": 7},
                                    "output": [call]}}),
        # second turn on the same connection, text only
        _ws("w", 2.0, {"type": "response.create", "model": "gpt-5-codex"}, client=True),
        _ws("w", 2.1, {"type": "response.completed",
                       "response": {"model": "gpt-5-codex", "output": []}}),
    ]
    w = PARSERS["openai"](recs, kind)
    assert w.recognized == 2 and w.unrecognized == 0 and w.api_errors == 0
    assert w.api_requests == w.api_requests_authed == 1
    assert [c.tool_id for c in w.tool_calls] == ["call_1"]
    assert w.tool_calls[0].kind == "shell" and w.tool_calls[0].ts == 1.4
    assert w.primary_model() == "gpt-5-codex"

    from mrh.analyze.session import capture_problems
    assert capture_problems({"status": "exited"}, w, True, True, False) == []


def test_credential_not_sent():
    from mrh.adapters import get_adapter
    from mrh.analyze.session import capture_problems
    from mrh.analyze.wire import PARSERS

    url = "https://api.openai.com/v1/responses"
    recs = [{"direction": "request", "flow_id": "a", "method": "POST", "url": url, "headers": {}},
            {"direction": "response", "flow_id": "a", "status": 401, "url": url, "headers": {},
             "body": '{"error": {"message": "Missing bearer or basic authentication in header"}}'}]
    w = PARSERS["openai"](recs, get_adapter("codex").tool_kind)
    p = capture_problems({"status": "exited"}, w, True, True, False)
    assert "credential_not_sent" in p and "api_errors_only" in p

    from mrh.capture.redact import carries_credential
    assert carries_credential({"headers": {"x-goog-api-key": "<redacted:sha256:1>"}})
    assert carries_credential({"headers": {}, "url": "https://x/y?key=%3Credacted:sha256:1%3E"}) is False
    assert carries_credential({"headers": {}, "url": "https://x/y?key=<redacted:sha256:1>"})


def test_codex_key_alias(monkeypatch, tmp_path):
    from mrh.adapters import get_adapter
    from mrh.capture.session import build_env

    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-value")
    env, rec = build_env(get_adapter("codex"), tmp_path, 1, tmp_path / "ca.pem", {})
    assert env["CODEX_API_KEY"] == "sk-test-value"
    assert rec["CODEX_API_KEY"] == "<set from OPENAI_API_KEY>"
    assert "sk-test-value" not in str(rec)
    # an explicit CODEX_API_KEY wins
    monkeypatch.setenv("CODEX_API_KEY", "sk-other")
    env, _ = build_env(get_adapter("codex"), tmp_path, 1, tmp_path / "ca.pem", {})
    assert env["CODEX_API_KEY"] == "sk-other"
    # other adapters are unaffected
    env, _ = build_env(get_adapter("claude-code"), tmp_path, 1, tmp_path / "ca.pem", {})
    assert "CODEX_API_KEY" not in env


def test_scrub_echoed_key(tmp_path):
    from mrh.capture.redact import fingerprint, scrub_files, scrub_text

    key = "sk-dummy"
    body = '{"error": {"message": "Incorrect API key provided: sk-dummy. You can find"}}'
    out = scrub_text(body, [key])
    assert key not in out and fingerprint(key) in out
    # a masked echo of a real key is removed even though it is not the value
    masked = "Incorrect API key provided: sk-proj-ab12**********wxyz. You can"
    assert "wxyz" not in scrub_text(masked, [])
    # short values are not treated as secrets
    assert scrub_text("a dummy x", ["dummy"]) == "a dummy x"

    (tmp_path / "sub").mkdir()
    (tmp_path / "wire.jsonl").write_text(body)
    (tmp_path / "sub" / "clean.txt").write_text("nothing here")
    assert scrub_files(tmp_path, [key]) == ["wire.jsonl"]
    assert key not in (tmp_path / "wire.jsonl").read_text()


def test_gemini_settings_in_home(tmp_path):
    import json as _j

    from mrh.adapters import get_adapter

    ctx = tmp_path / "context"
    ctx.mkdir()
    spec = get_adapter("gemini-cli").prepare(
        {"prompt": {"id": "t", "text": "hi"}, "arm": {"approval_mode": "yolo"}}, tmp_path / "work", ctx)
    # 0.67 skips a system-settings file whose dir is not root-owned
    assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" not in spec.env
    assert spec.env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
    settings = _j.loads(spec.home_files[".gemini/settings.json"])
    assert settings == _j.loads((ctx / "gemini-settings.json").read_text())


def test_codex_code_mode_exec():
    from mrh.adapters import get_adapter
    from mrh.adapters.codex import code_mode_commands
    from mrh.events import ToolCall

    assert code_mode_commands('const r = await tools.exec_command({cmd:"echo hi"});text(r.output);\n') == ["echo hi"]
    assert code_mode_commands("await tools.exec_command({ cmd: 'printf \"%s\" it\\'s' })") == ['printf "%s" it\'s']
    assert code_mode_commands('tools.exec_command({cmd:"a \\"q\\""}); tools.exec_command({cmd:`ls -l`})') == [
        'a "q"', "ls -l"]
    assert code_mode_commands(None) == []

    ad = get_adapter("codex")
    js = 'const result = await tools.exec_command({cmd:"echo hi"});text(result.output);\n'
    wire = ToolCall(tool_id="call_1", name="exec", kind=ad.tool_kind("exec"), input={"input": js},
                    source="wire")
    stream = ToolCall(tool_id="item_1", name="shell", kind="shell",
                      input={"command": "/bin/bash -lc 'echo hi'"}, source="stream")
    assert wire.kind == "shell"
    assert ad.shell_command(wire) == ad.shell_command(stream) == "echo hi"


def test_sandbox_failure_flag():
    from mrh.analyze.session import SANDBOX_FAILURE, capture_problems

    msg = ("bwrap: No permissions to create a new namespace, likely because the kernel does not "
           "allow non-privileged user namespaces.")
    assert SANDBOX_FAILURE.search(msg)
    assert not SANDBOX_FAILURE.search("hi\n")

    class W:
        api_errors = recognized = api_requests = api_requests_authed = 0
        requests = responses = api_responses = 1
        unrecognized = 0
    assert "agent_sandbox_failed" in capture_problems({"status": "exited"}, W(), True, True, False, True)


def test_codex_telemetry_off(tmp_path):
    from mrh.adapters import get_adapter

    ctx = tmp_path / "context"
    ctx.mkdir()
    spec = get_adapter("codex").prepare(
        {"prompt": {"id": "t", "text": "hi"}, "arm": {"config": {"otel.exporter": "otlp-http"}}},
        tmp_path / "work", ctx)
    pairs = {spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-c"}
    assert {"analytics.enabled=false", "otel.metrics_exporter=none",
            "otel.trace_exporter=none"} <= pairs
    assert "otel.exporter=otlp-http" in pairs and "otel.exporter=none" not in pairs
