#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$PROJECT_ROOT/.env.local}"
COMPOSE_FILE="${COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.local.yml}"
TAIL_LINES="${TAIL_LINES:-220}"
OLLAMA_HOST_URL="${OLLAMA_HOST_URL:-http://127.0.0.1:11434}"
OLLAMA_CONTAINER_HOST="${OLLAMA_CONTAINER_HOST:-host.docker.internal}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
LLM_MODEL="${LLM_MODEL:-qwen2.5-coder:14b}"
EMBEDDING_MODEL="${EMBEDDING_MODEL:-nomic-embed-text:latest}"
CURRENT_STEP="initialization"
SHOW_LOGS_ON_ERROR=1
NO_BUILD=0
NO_WARM=0

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

usage() {
  cat <<'USAGE'
Usage:
  scripts/local-up-test.sh [command] [options]

Commands:
  up          Full local startup and verification (default)
  test        Run health, network, LLM, embedding, and local AWS tests
  logs        Print recent Cognee indexer logs
  follow      Follow Cognee indexer logs live
  diagnose    Print service state, environment, networking, and logs
  down        Stop local containers without deleting volumes
  help        Show this help

Options:
  --no-build  Do not rebuild images during "up"
  --no-warm   Do not warm the Qwen model during "up"
  --tail N    Number of log lines to print (default: 220)
USAGE
}

log() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }
ok() { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"; }
compose_available() { [[ -f "$ENV_FILE" && -f "$COMPOSE_FILE" ]]; }

show_compose_status() {
  compose_available && "${COMPOSE[@]}" ps -a || true
}

show_indexer_logs() {
  local tail_lines="${1:-$TAIL_LINES}"

  log "Cognee indexer state"
  if compose_available; then
    local current_id
    current_id="$("${COMPOSE[@]}" ps -q cognee-indexer 2>/dev/null || true)"
    if [[ -n "$current_id" ]]; then
      docker inspect "$current_id" \
        --format 'name={{.Name}} running={{.State.Running}} status={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} restarts={{.RestartCount}} error={{.State.Error}}' \
        || true
    else
      warn "No current cognee-indexer container found."
    fi
  fi

  log "Recent Cognee indexer logs"
  compose_available && "${COMPOSE[@]}" logs --timestamps --tail="$tail_lines" cognee-indexer || true

  log "Older or one-off indexer containers"
  local found=0
  while IFS=$'\t' read -r id name status; do
    [[ -n "$id" ]] || continue
    found=1
    printf '\n--- %s | %s ---\n' "$name" "$status"
    docker logs --timestamps --tail="$tail_lines" "$id" 2>&1 || true
  done < <(
    docker ps -a --filter 'name=cognee-indexer' \
      --format '{{.ID}}\t{{.Names}}\t{{.Status}}' 2>/dev/null || true
  )

  [[ "$found" -eq 1 ]] || echo "No indexer containers found."
}

on_error() {
  local line="$1" status="$2"
  trap - ERR
  set +e
  printf '\n\033[1;31mFAILED\033[0m at step: %s\n' "$CURRENT_STEP" >&2
  printf 'Line: %s | exit status: %s\n' "$line" "$status" >&2
  show_compose_status
  [[ "$SHOW_LOGS_ON_ERROR" -eq 1 ]] && show_indexer_logs "$TAIL_LINES"
  exit "$status"
}
trap 'on_error "$LINENO" "$?"' ERR

parse_options() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --no-build) NO_BUILD=1 ;;
      --no-warm) NO_WARM=1 ;;
      --tail)
        shift
        [[ $# -gt 0 ]] || die "--tail requires a number"
        TAIL_LINES="$1"
        ;;
      --tail=*) TAIL_LINES="${1#*=}" ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1" ;;
    esac
    shift
  done
}

preflight() {
  CURRENT_STEP="preflight"
  cd "$PROJECT_ROOT"
  for cmd in docker curl jq python3 ollama ss; do require_command "$cmd"; done
  docker info >/dev/null 2>&1 || die "Docker is not running or is inaccessible."
  [[ -f "$ENV_FILE" ]] || die "Missing environment file: $ENV_FILE"
  [[ -f "$COMPOSE_FILE" ]] || die "Missing Compose file: $COMPOSE_FILE"
}

wire_local_environment() {
  CURRENT_STEP="writing local-only environment settings"
  log "Wire local Cognee to host Ollama"

  ENV_FILE="$ENV_FILE" LLM_MODEL_VALUE="$LLM_MODEL" EMBEDDING_MODEL_VALUE="$EMBEDDING_MODEL" python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["ENV_FILE"])
llm_model = os.environ["LLM_MODEL_VALUE"]
embedding_model = os.environ["EMBEDDING_MODEL_VALUE"]
settings = {
    "APP_MODE": "local",
    "MOCK_BACKEND": "0",
    "MODEL_PROVIDER": "ollama",
    "CHAT_MODEL_PROVIDER": "ollama",
    "OLLAMA_MODEL": llm_model,
    "OLLAMA_URL": "http://host.docker.internal:11434",
    "LLM_PROVIDER": "ollama",
    "LLM_MODEL": llm_model,
    "LLM_ENDPOINT": "http://host.docker.internal:11434/v1",
    "LLM_API_KEY": "ollama",
    "LLM_INSTRUCTOR_MODE": "json_mode",
    "COGNEE_LLM_PROVIDER": "ollama",
    "COGNEE_LLM_MODEL": llm_model,
    "COGNEE_LLM_ENDPOINT": "http://host.docker.internal:11434/v1",
    "COGNEE_LLM_API_KEY": "ollama",
    "EMBEDDING_PROVIDER": "ollama",
    "EMBEDDING_MODEL": embedding_model,
    "EMBEDDING_ENDPOINT": "http://host.docker.internal:11434/api/embed",
    "EMBEDDING_DIMENSIONS": "768",
    "EMBEDDING_API_KEY": "ollama",
    "COGNEE_EMBEDDING_PROVIDER": "ollama",
    "COGNEE_EMBEDDING_MODEL": embedding_model,
    "COGNEE_EMBEDDING_ENDPOINT": "http://host.docker.internal:11434/api/embed",
    "COGNEE_EMBEDDING_DIMENSIONS": "768",
    "COGNEE_EMBEDDING_API_KEY": "ollama",
    "COGNEE_SKIP_CONNECTION_TEST": "true",
}
text = path.read_text(encoding="utf-8") if path.exists() else ""
output, seen = [], set()
for line in text.splitlines():
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in line:
        key = line.split("=", 1)[0].strip()
        if key in settings:
            output.append(f"{key}={settings[key]}")
            seen.add(key)
            continue
    output.append(line)
if output and output[-1] != "": output.append("")
for key, value in settings.items():
    if key not in seen: output.append(f"{key}={value}")
path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
PY

  grep -E '^(APP_MODE|MOCK_BACKEND|MODEL_PROVIDER|OLLAMA_MODEL|OLLAMA_URL|LLM_PROVIDER|LLM_MODEL|LLM_ENDPOINT|EMBEDDING_PROVIDER|EMBEDDING_MODEL|EMBEDDING_ENDPOINT|COGNEE_SKIP_CONNECTION_TEST)=' "$ENV_FILE"
}

ensure_ollama() {
  CURRENT_STEP="starting host Ollama"
  log "Start or verify host Ollama"
  export OLLAMA_MODELS="${OLLAMA_MODELS:-$PROJECT_ROOT/.local/ollama-models}"
  export OLLAMA_HOST="0.0.0.0:${OLLAMA_PORT}"

  if ! curl --connect-timeout 2 --max-time 5 -fsS "$OLLAMA_HOST_URL/api/tags" >/dev/null 2>&1; then
    echo "Starting Ollama..."
    nohup ollama serve >/tmp/cognimoss-ollama.log 2>&1 &
    for _ in $(seq 1 30); do
      curl --connect-timeout 2 --max-time 5 -fsS "$OLLAMA_HOST_URL/api/tags" >/dev/null 2>&1 && break
      sleep 1
    done
  fi

  curl --connect-timeout 3 --max-time 10 -fsS "$OLLAMA_HOST_URL/api/tags" >/dev/null || {
    tail -100 /tmp/cognimoss-ollama.log 2>/dev/null || true
    die "Ollama did not start."
  }

  ss -ltnp | grep ":${OLLAMA_PORT}" || true
  echo "Installed models:"
  curl -fsS "$OLLAMA_HOST_URL/api/tags" | jq -r '.models[].name'

  for model in "$LLM_MODEL" "$EMBEDDING_MODEL"; do
    curl -fsS "$OLLAMA_HOST_URL/api/tags" | jq -e --arg model "$model" '.models[] | select(.name == $model)' >/dev/null \
      || die "Missing model: $model. Run: ollama pull $model"
  done
  ok "Host Ollama is reachable."
}

warm_model() {
  CURRENT_STEP="warming Qwen model"
  log "Warm Qwen model"
  curl --connect-timeout 5 --max-time 300 -fsS "$OLLAMA_HOST_URL/api/generate" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$LLM_MODEL\",\"prompt\":\"Reply with exactly OK\",\"stream\":false,\"keep_alive\":\"30m\",\"options\":{\"temperature\":0,\"num_predict\":8}}" \
    | jq '{response,done,load_duration,total_duration}'
  ollama ps
  ok "LLM model is warm."
}

start_stack() {
  if [[ "$NO_BUILD" -eq 0 ]]; then
    CURRENT_STEP="building local images"
    log "Build local container images"
    "${COMPOSE[@]}" build
  fi

  CURRENT_STEP="starting local containers"
  log "Start complete local stack"
  "${COMPOSE[@]}" up -d --force-recreate

  for _ in $(seq 1 60); do
    curl --connect-timeout 2 --max-time 5 -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break
    sleep 2
  done
  show_compose_status
}

test_frontend() {
  CURRENT_STEP="testing frontend health"
  log "Frontend health"
  curl --connect-timeout 3 --max-time 10 -fsS http://127.0.0.1:8000/health | jq
  ok "Frontend health check passed."
}

detect_bridge() {
  local id network network_id bridge
  id="$("${COMPOSE[@]}" ps -q cognee-indexer 2>/dev/null || true)"
  [[ -n "$id" ]] || return 1
  network="$(docker inspect "$id" --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{end}}')"
  network_id="$(docker network inspect "$network" --format '{{.Id}}')"
  bridge="$(docker network inspect "$network" --format '{{index .Options "com.docker.network.bridge.name"}}')"
  [[ -n "$bridge" && "$bridge" != "<no value>" ]] || bridge="br-${network_id:0:12}"
  printf '%s\n' "$bridge"
}

print_firewall_help() {
  local bridge="$(detect_bridge 2>/dev/null || true)"
  warn "NixOS is probably blocking Docker-to-Ollama traffic."
  if [[ -n "$bridge" ]]; then
    echo "Detected Compose bridge: $bridge"
    echo "Add this to configuration.nix:"
    printf 'networking.firewall.interfaces."%s".allowedTCPPorts = [ %s ];\n' "$bridge" "$OLLAMA_PORT"
    echo "Then run: sudo nixos-rebuild test"
  fi
}

test_container_tcp() {
  CURRENT_STEP="testing Docker-to-Ollama TCP connectivity"
  log "Container-to-host Ollama TCP"
  set +e
  "${COMPOSE[@]}" run --rm --no-deps cognee-indexer python - <<PY
import socket, time
host = "${OLLAMA_CONTAINER_HOST}"
port = ${OLLAMA_PORT}
print("Resolved addresses:")
print(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
started = time.time()
with socket.create_connection((host, port), timeout=5):
    print(f"TCP connection successful in {time.time() - started:.3f}s")
PY
  local status=$?
  set -e
  [[ "$status" -eq 0 ]] || { print_firewall_help; return "$status"; }
  ok "Container can reach host Ollama."
}

test_structured_output() {
  CURRENT_STEP="testing OpenAI-compatible structured output"
  log "OpenAI-compatible structured output"
  "${COMPOSE[@]}" run --rm --no-deps cognee-indexer python - <<'PY'
import json, os, time, requests
endpoint = os.environ["LLM_ENDPOINT"].rstrip("/") + "/chat/completions"
model = os.environ["LLM_MODEL"]
payload = {
    "model": model,
    "messages": [{"role": "user", "content": 'Return only valid JSON matching {"status":"ok"}.'}],
    "response_format": {"type": "json_object"},
    "temperature": 0,
    "max_tokens": 64,
    "stream": False,
}
print("Endpoint:", endpoint)
print("Model:", model)
started = time.time()
response = requests.post(endpoint, json=payload, timeout=(5, 300))
print("HTTP:", response.status_code)
print("Seconds:", round(time.time() - started, 2))
response.raise_for_status()
content = response.json()["choices"][0]["message"]["content"]
print("Response:", content)
parsed = json.loads(content)
if parsed.get("status") != "ok": raise RuntimeError(f"Unexpected response: {parsed!r}")
print("Structured-output test passed")
PY
  ok "Structured-output request passed."
}

test_embedding() {
  CURRENT_STEP="testing Ollama embeddings"
  log "Embedding endpoint"
  "${COMPOSE[@]}" run --rm --no-deps cognee-indexer python - <<'PY'
import os, requests
endpoint = os.environ["EMBEDDING_ENDPOINT"]
model = os.environ["EMBEDDING_MODEL"]
response = requests.post(endpoint, json={"model": model, "input": ["Cognimoss local embedding health test"], "keep_alive": "30m"}, timeout=(5, 180))
print("Endpoint:", endpoint)
print("Model:", model)
print("HTTP:", response.status_code)
response.raise_for_status()
embeddings = response.json().get("embeddings") or []
if not embeddings: raise RuntimeError("No embeddings returned")
dimensions = len(embeddings[0])
print("Embedding count:", len(embeddings))
print("Embedding dimensions:", dimensions)
if dimensions != 768: raise RuntimeError(f"Expected 768 dimensions, got {dimensions}")
print("Embedding test passed")
PY
  ok "Embedding request passed."
}

show_effective_environment() {
  CURRENT_STEP="reading effective indexer environment"
  log "Effective Cognee environment"
  "${COMPOSE[@]}" exec -T cognee-indexer sh -lc 'env | grep -E "^(LLM_|COGNEE_LLM_|EMBEDDING_|COGNEE_EMBEDDING_|COGNEE_SKIP_CONNECTION_TEST)=" | sort'
}

test_local_aws() {
  CURRENT_STEP="testing local Moto resources"
  log "Local AWS emulator"
  if ! command -v aws >/dev/null 2>&1; then warn "AWS CLI unavailable; skipping direct Moto listing."; return 0; fi
  AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1 aws --endpoint-url http://127.0.0.1:5000 dynamodb list-tables | jq
  AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1 aws --endpoint-url http://127.0.0.1:5000 sqs list-queues | jq
  AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1 aws --endpoint-url http://127.0.0.1:5000 s3api list-buckets | jq '.Buckets'
  ok "Moto resource tests passed."
}

run_tests() {
  test_frontend
  test_container_tcp
  test_structured_output
  test_embedding
  show_effective_environment
  test_local_aws
}

run_up() {
  preflight
  wire_local_environment
  ensure_ollama
  [[ "$NO_WARM" -eq 1 ]] || warm_model
  start_stack
  run_tests
  log "Final container status"
  show_compose_status
  log "Recent Cognee indexer logs"
  "${COMPOSE[@]}" logs --timestamps --tail=80 cognee-indexer || true
  printf '\n\033[1;32mALL LOCAL STARTUP TESTS PASSED\033[0m\n'
  echo "Dashboard: http://127.0.0.1:8000"
  echo "After submitting an indexing run: scripts/local-up-test.sh follow"
}

run_diagnose() {
  preflight
  log "Host Ollama"
  ss -ltnp | grep ":${OLLAMA_PORT}" || true
  curl --connect-timeout 3 --max-time 10 -fsS "$OLLAMA_HOST_URL/api/tags" | jq -r '.models[].name' || true
  log "Container status"
  show_compose_status
  show_effective_environment || true
  test_container_tcp || true
  show_indexer_logs "$TAIL_LINES"
}

run_logs() { preflight; SHOW_LOGS_ON_ERROR=0; show_indexer_logs "$TAIL_LINES"; }
run_follow() {
  preflight
  SHOW_LOGS_ON_ERROR=0
  log "Following Cognee indexer logs"
  echo "Ctrl+C stops viewing only; the indexer keeps running."
  "${COMPOSE[@]}" logs -f --timestamps --tail="$TAIL_LINES" cognee-indexer
}
run_down() { preflight; SHOW_LOGS_ON_ERROR=0; "${COMPOSE[@]}" down; ok "Containers stopped; volumes preserved."; }

COMMAND="${1:-up}"
[[ $# -eq 0 ]] || shift
parse_options "$@"

case "$COMMAND" in
  up) run_up ;;
  test) preflight; run_tests ;;
  logs) run_logs ;;
  follow) run_follow ;;
  diagnose) run_diagnose ;;
  down) run_down ;;
  help|-h|--help) usage ;;
  *) die "Unknown command: $COMMAND" ;;
esac
