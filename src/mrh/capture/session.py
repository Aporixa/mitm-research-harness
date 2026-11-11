"""Run one agent session under capture.

Layout of a session directory:

    manifest.json     what ran: harness/agent/tool versions, argv, env names,
                      trial spec, timings, exit status, file hashes
    context/          everything the agent could see: prompt.txt, argv.json,
                      planted/ files, settings.json
    wire.jsonl        model API traffic (credentials redacted at capture)
    strace.log        syscall trace of the agent and all its children
    stream.jsonl      the agent's consumer-facing output
    stderr.log        agent stderr
    mitm.log          proxy log

Replaces v1's start-session.sh. Differences:
  * The environment is built from an allowlist (adapter.passthrough_env
    plus credentials by name). v1 inherited the full operator environment
    and dumped it to env.txt.
  * The proxy CA is passed to the agent through env only (adapter.proxy_env:
    NODE_EXTRA_CA_CERTS, SSL_CERT_FILE, ...). v1's setup installed the CA
    system-wide on the capture host.
  * Only the adapter's model-API hosts are intercepted (--allow-hosts);
    other TLS traffic is tunneled untouched.
  * The run is designed for a throwaway container (docker/), so the
    agent's working directory and $HOME are fresh per trial.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from mrh import SCHEMA_VERSION, __version__
from mrh.adapters.base import Adapter
from mrh.capture.redact import scrub_files
from mrh.capture.strace import STRACE_ARGS
from mrh.util import free_port, sha256_file, write_json_atomic

ADDON = Path(__file__).with_name("mitm_addon.py")


def _tool_version(argv: list[str]) -> str | None:
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return ((r.stdout or r.stderr).strip().splitlines() or [""])[0]


def _wait_for_port(port: int, proc: subprocess.Popen[bytes], timeout: float = 20.0) -> None:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"mitmdump exited early with code {proc.returncode}")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"mitmdump did not listen on {port} within {timeout}s")


def build_env(adapter: Adapter, home: Path, proxy_port: int, ca_cert: Path,
              extra: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (env for the agent, env record for the manifest).

    Credential values are passed through but recorded only as "<set>".
    """
    env: dict[str, str] = {}
    record: dict[str, str] = {}
    for name in adapter.passthrough_env:
        if name in os.environ:
            env[name] = os.environ[name]
            record[name] = os.environ[name] if name != "HOME" else "<per-trial>"
    for name in adapter.credential_env:
        if os.environ.get(name):
            env[name] = os.environ[name]
            record[name] = "<set>"
    for name, src in adapter.credential_aliases.items():
        if name not in env and env.get(src):
            env[name] = env[src]
            record[name] = f"<set from {src}>"
    env["HOME"] = str(home)
    capture = adapter.proxy_env(f"http://127.0.0.1:{proxy_port}", ca_cert) | adapter.noise_env
    env.update(capture)
    record.update(capture)
    for k, v in extra.items():
        env[k] = v
        record[k] = "<set>" if k in adapter.credential_env else v
    return env, record


def allow_hosts_regex(hosts: tuple[str, ...]) -> str:
    """mitmproxy --allow-hosts pattern: intercept only the model API hosts.
    Everything else (package registries, update checks, OAuth) is tunneled
    untouched, so the agent does not need to trust the CA for it and its
    bodies never reach the capture."""
    alts = "|".join(re.escape(h) for h in hosts)
    return rf"^(?:.+[.-])?(?:{alts})(?::\d+)?$"


def run_session(adapter: Adapter, trial: dict[str, Any], out: Path,
                timeout_s: int = 300, use_strace: bool = True) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    context = out / "context"
    scratch = Path(tempfile.mkdtemp(prefix="harness-trial-"))
    workdir = scratch / "work"
    home = scratch / "home"
    confdir = scratch / "mitm"
    for d in (workdir, home, confdir):
        d.mkdir(parents=True)

    manifest: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "harness_version": __version__,
        "adapter": adapter.name,
        "provider": adapter.provider,
        "adapter_experimental": adapter.experimental,
        "wire_format": adapter.wire_format,
        "image": os.environ.get("HARNESS_IMAGE"),
        "harness_commit": os.environ.get("HARNESS_GIT_COMMIT"),
        "harness_src_hash": os.environ.get("HARNESS_SRC_HASH"),
        "agent_version": adapter.version(),
        "trial": {k: v for k, v in trial.items() if k != "arm"} | {
            "arm": (trial.get("arm") or {}).get("id")},
        "host": {"python": sys.version.split()[0], "platform": platform.platform(),
                 "in_container": Path("/.dockerenv").exists()},
        "tools": {"strace": _tool_version(["strace", "-V"]),
                  "mitmdump": _tool_version(["mitmdump", "--version"])},
        "started": time.time(),
    }

    mitm_proc: subprocess.Popen[bytes] | None = None
    try:
        port = free_port()
        mitm_log = (out / "mitm.log").open("wb")
        mitm_proc = subprocess.Popen(
            ["mitmdump", "--listen-host", "127.0.0.1", "--listen-port", str(port),
             "--set", f"confdir={confdir}", "-s", str(ADDON),
             "--set", f"harness_out={out / 'wire.jsonl'}",
             "--set", f"harness_hosts={','.join(adapter.wire_hosts)}",
             "--allow-hosts", allow_hosts_regex(adapter.wire_hosts), "-q"],
            stdout=mitm_log, stderr=subprocess.STDOUT, start_new_session=True)
        _wait_for_port(port, mitm_proc)
        ca = confdir / "mitmproxy-ca-cert.pem"
        for _ in range(50):
            if ca.exists():
                break
            time.sleep(0.1)
        else:
            # Without the CA the agent cannot trust the proxy: every model
            # call fails TLS and the session looks like an agent that did
            # nothing. Stop here instead.
            raise RuntimeError(f"mitmproxy CA not created in {confdir} within 5s")

        spec = adapter.prepare(trial, workdir, context)
        for rel, content in spec.home_files.items():
            target = home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        env, env_record = build_env(adapter, home, port, ca, spec.env)
        argv = spec.argv
        if use_strace:
            argv = ["strace", *STRACE_ARGS, "-o", str(out / "strace.log"), "--", *argv]
        manifest.update({"argv": argv, "env": env_record, "proxy_port": port})

        t0 = time.time()
        with (out / "stream.jsonl").open("wb") as so, (out / "stderr.log").open("wb") as se:
            proc = subprocess.Popen(argv, cwd=spec.cwd, env=env, stdin=subprocess.PIPE,
                                    stdout=so, stderr=se, start_new_session=True)
            try:
                proc.communicate(input=(spec.stdin_text or "").encode(), timeout=timeout_s)
                status = "exited"
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                status = "timeout"
        manifest.update({"status": status, "exit_code": proc.returncode,
                         "duration_s": round(time.time() - t0, 3)})
    except Exception as exc:  # recorded, then re-raised for the runner
        manifest.update({"status": "harness_error", "error": repr(exc)})
        raise
    finally:
        if mitm_proc is not None and mitm_proc.poll() is None:
            os.killpg(mitm_proc.pid, signal.SIGTERM)
            try:
                mitm_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(mitm_proc.pid, signal.SIGKILL)
        secrets = [os.environ[n] for n in (*adapter.credential_env, *adapter.credential_aliases.values())
                   if os.environ.get(n)]
        manifest["scrubbed_files"] = scrub_files(out, secrets)
        manifest["finished"] = time.time()
        manifest["files"] = {
            str(p.relative_to(out)): sha256_file(p)
            for p in sorted(out.rglob("*")) if p.is_file() and p.name != "manifest.json"}
        write_json_atomic(out / "manifest.json", manifest)
        shutil.rmtree(scratch, ignore_errors=True)  # includes the per-trial CA key
    return manifest
