#!/usr/bin/env bash
# Run an mrh command in the capture container.
#
#   docker/run.sh doctor
#   docker/run.sh adapters
#   docker/run.sh experiment run post-refusal-fabrication/experiment.yaml --out /runs --limit 2
#
# Agent versions (empty leaves an agent out of the image):
#   CLAUDE_CODE_VERSION (default 2.1.317), CODEX_VERSION, GEMINI_CLI_VERSION
#   e.g. CODEX_VERSION=0.166.0 docker/run.sh doctor
#
# ./experiments is mounted read-only at /experiments, and ./runs at /runs.
# The credential comes from ./.env (see .env.example) or the shell, and is
# passed by NAME (-e VAR), so its value never appears in argv or shell
# history. Note that `docker inspect` on a running container does show env
# values, as with any docker env var.
# The container runs as the host uid so files in ./runs stay yours. Each
# trial gets its own HOME, so the /tmp HOME here is only for the CLI itself.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
cc="${CLAUDE_CODE_VERSION-2.1.317}"
codex="${CODEX_VERSION-}"
gemini="${GEMINI_CLI_VERSION-}"
tag=""
[[ -n "$cc" ]] && tag+="cc-${cc}_"
[[ -n "$codex" ]] && tag+="codex-${codex}_"
[[ -n "$gemini" ]] && tag+="gemini-${gemini}_"
tag="${tag%_}"
[[ -n "$tag" ]] || { echo "set at least one agent version" >&2; exit 1; }
image="mitm-research-harness:${tag}"
commit="$(git -C "$here" rev-parse --short=12 HEAD 2>/dev/null || echo uncommitted)"
if [[ -n "$(git -C "$here" status --porcelain 2>/dev/null)" ]]; then commit+="-dirty"; fi

# Hash of everything the image copies in. The tag only names agent versions,
# so without this a harness change would silently reuse a stale image.
src_hash="$(cd "$here" && find pyproject.toml README.md src docs docker -type f \
  -not -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)"
built_hash="$(docker image inspect -f '{{index .Config.Labels "mrh.src"}}' "$image" 2>/dev/null || true)"
if [[ "$built_hash" != "$src_hash" ]]; then
  [[ -n "$built_hash" ]] && echo "harness source changed since $image was built; rebuilding" >&2
  docker build -f "$here/docker/Dockerfile" \
    --build-arg "CLAUDE_CODE_VERSION=$cc" --build-arg "CODEX_VERSION=$codex" \
    --build-arg "GEMINI_CLI_VERSION=$gemini" \
    --build-arg "HARNESS_GIT_COMMIT=$commit" --build-arg "HARNESS_IMAGE=$image" \
    --build-arg "HARNESS_SRC_HASH=$src_hash" \
    -t "$image" "$here"
fi

mkdir -p "$here/runs"
# Every credential name any adapter reads (see `mrh adapters`), plus the
# Vertex AI routing settings for Gemini CLI.
cred_names=(ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN
            OPENAI_API_KEY CODEX_API_KEY
            GEMINI_API_KEY GOOGLE_API_KEY
            GOOGLE_GENAI_USE_VERTEXAI GOOGLE_CLOUD_PROJECT GOOGLE_CLOUD_LOCATION)

# Credentials from .env (default: repo root; override with HARNESS_ENV_FILE).
# The file is parsed, never sourced, so it cannot run code. Only the names
# above are read; every other line is ignored. A variable already set in the
# shell wins over the file. Values are never printed.
env_file="${HARNESS_ENV_FILE:-$here/.env}"
if [[ -f "$env_file" ]]; then
  perms="$(stat -c '%a' "$env_file" 2>/dev/null || stat -f '%Lp' "$env_file")"
  if [[ "${perms: -2}" != "00" ]]; then
    echo "warning: $env_file is readable by group/others (mode $perms); run: chmod 600 $env_file" >&2
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Z_][A-Z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[2]}" val="${BASH_REMATCH[3]}"
    [[ " ${cred_names[*]} " == *" $key "* ]] || continue
    [[ -n "${!key:-}" ]] && continue
    if [[ "$val" =~ ^\"(.*)\"[[:space:]]*$ || "$val" =~ ^\'(.*)\'[[:space:]]*$ ]]; then
      val="${BASH_REMATCH[1]}"
    else
      val="${val%%[[:space:]]#*}"            # strip an inline " # comment"
      val="${val%"${val##*[![:space:]]}"}"   # trim trailing whitespace
    fi
    [[ -n "$val" ]] && export "$key=$val"
  done < "$env_file"
fi

cred_args=()
for name in "${cred_names[@]}"; do
  if [[ -n "${!name:-}" ]]; then cred_args+=(-e "$name"); fi
done
if [[ ${#cred_args[@]} -eq 0 && "${1:-}" != "doctor" ]]; then
  echo "warning: no credential set; add one of ${cred_names[*]} to $env_file" >&2
fi

exec docker run --rm -i \
  --cap-add SYS_PTRACE \
  --security-opt no-new-privileges \
  --memory 4g --pids-limit 512 \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  "${cred_args[@]}" \
  -v "$here/experiments:/experiments:ro" \
  -v "$here/runs:/runs" \
  "$image" "$@"
