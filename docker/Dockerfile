# Capture image: pinned agent versions, plus strace, mitmproxy and the harness.
#
#   docker build -f docker/Dockerfile \
#       --build-arg CLAUDE_CODE_VERSION=2.1.317 \
#       --build-arg CODEX_VERSION=0.166.0 \
#       --build-arg GEMINI_CLI_VERSION=0.67.0 \
#       -t mitm-research-harness:cc-2.1.317_codex-0.166.0_gemini-0.67.0 .
#
# Each *_VERSION arg installs that agent at that exact version; an empty arg
# leaves the agent out. At least one must be set. docker/run.sh derives the
# tag from the versions, so a run can be tied to exactly one build.
#
# The proxy CA is generated per trial inside the container and trusted only
# through env vars on the agent process (NODE_EXTRA_CA_CERTS, SSL_CERT_FILE,
# CODEX_CA_CERTIFICATE, ...). Nothing is installed into the system trust
# store, here or on the host.
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=
ARG CODEX_VERSION=
ARG GEMINI_CLI_VERSION=
ARG HARNESS_GIT_COMMIT=unknown
ARG HARNESS_IMAGE=mitm-research-harness:local
ARG HARNESS_SRC_HASH=unknown
RUN test -n "$CLAUDE_CODE_VERSION$CODEX_VERSION$GEMINI_CLI_VERSION" \
 || (echo "set at least one of CLAUDE_CODE_VERSION, CODEX_VERSION, GEMINI_CLI_VERSION" && exit 1)

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      strace python3 python3-venv ca-certificates git procps \
 && rm -rf /var/lib/apt/lists/*

RUN set -e; pkgs=""; \
    [ -n "$CLAUDE_CODE_VERSION" ] && pkgs="$pkgs @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"; \
    [ -n "$CODEX_VERSION" ] && pkgs="$pkgs @openai/codex@${CODEX_VERSION}"; \
    [ -n "$GEMINI_CLI_VERSION" ] && pkgs="$pkgs @google/gemini-cli@${GEMINI_CLI_VERSION}"; \
    npm install -g $pkgs && npm cache clean --force

COPY pyproject.toml README.md /opt/harness-src/
COPY src /opt/harness-src/src
COPY docs /opt/harness-src/docs
RUN python3 -m venv /opt/harness \
 && /opt/harness/bin/pip install --no-cache-dir "/opt/harness-src[capture]"

# Unprivileged user. Trials get a fresh $HOME and working directory under /tmp.
RUN useradd --create-home --shell /bin/bash agent \
 && mkdir -p /runs /experiments && chown agent:agent /runs /experiments
USER agent
# docker/run.sh rebuilds when this differs from the current source.
LABEL mrh.src=${HARNESS_SRC_HASH}
# Recorded in every session manifest.
ENV PATH=/opt/harness/bin:$PATH \
    HARNESS_GIT_COMMIT=${HARNESS_GIT_COMMIT} \
    HARNESS_IMAGE=${HARNESS_IMAGE} \
    HARNESS_SRC_HASH=${HARNESS_SRC_HASH} \
    MRH_CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION} \
    MRH_CODEX_VERSION=${CODEX_VERSION} \
    MRH_GEMINI_CLI_VERSION=${GEMINI_CLI_VERSION}
WORKDIR /experiments
ENTRYPOINT ["mrh"]
CMD ["doctor"]
