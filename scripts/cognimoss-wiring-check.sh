#!/usr/bin/env bash
# Cognimoss local wiring diagnostic
#
# Read-only apart from creating temporary probe containers and this report.
# It does not rebuild NixOS, edit firewall rules, restart services, or modify
# application containers.
#
# Usage:
#   cd ~/projects/github_coding_agent
#   sudo -v   # optional, lets the script read firewall rules
#   bash /path/to/cognimoss-wiring-check.sh
#
# Optional overrides:
#   REPO_DIR=/path/to/repo
#   COMPOSE_FILE=docker-compose.local.yml
#   ENV_FILE=.env.local
#   REPORT=/path/to/output.txt
#
# Exit code is always 0 so the report is not truncated by a failed test.

set -uo pipefail

REPO_DIR="${REPO_DIR:-${1:-$PWD}}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.local.yml}"
ENV_FILE="${ENV_FILE:-.env.local}"
STAMP="$(date +%Y%m%d-%H%M%S)"
REPORT="${REPORT:-$PWD/cognimoss-wiring-report-${STAMP}.txt}"

PASS=0
FAIL=0
WARN=0

mkdir -p "$(dirname "$REPORT")"
touch "$REPORT" || {
  echo "ERROR: cannot create report: $REPORT" >&2
  exit 1
}

exec > >(tee -a "$REPORT") 2>&1

section() {
  printf '\n\n================================================================\n'
  printf '== %s\n' "$1"
  printf '================================================================\n'
}

subsection() {
  printf '\n---- %s ----\n' "$1"
}

ok() {
  PASS=$((PASS + 1))
  printf '[PASS] %s\n' "$*"
}

bad() {
  FAIL=$((FAIL + 1))
  printf '[FAIL] %s\n' "$*"
}

warn() {
  WARN=$((WARN + 1))
  printf '[WARN] %s\n' "$*"
}

have() {
  command -v "$1" >/dev/null 2>&1
}

run() {
  printf '\n$'
  printf ' %q' "$@"
  printf '\n'
  "$@"
  local rc=$?
  printf '[exit=%s]\n' "$rc"
  return "$rc"
}

run_shell() {
  local command_text="$1"
  printf '\n$ %s\n' "$command_text"
  bash -o pipefail -c "$command_text"
  local rc=$?
  printf '[exit=%s]\n' "$rc"
  return "$rc"
}

redact_url_credentials() {
  sed -E 's#(https?://)[^/@:[:space:]]+:[^/@[:space:]]+@#\1<redacted>@#g'
}

container_id_for_service() {
  local service="$1"
  "${COMPOSE[@]}" ps -q "$service" 2>/dev/null | head -n1 || true
}

first_nonempty() {
  local value
  for value in "$@"; do
    if [[ -n "$value" ]]; then
      printf '%s' "$value"
      return 0
    fi
  done
  return 1
}

section "Cognimoss local wiring diagnostic"
printf 'Started:       %s\n' "$(date --iso-8601=seconds 2>/dev/null || date)"
printf 'User:          %s\n' "$(id)"
printf 'Host:          %s\n' "$(hostname)"
printf 'Repo:          %s\n' "$REPO_DIR"
printf 'Compose file:  %s\n' "$COMPOSE_FILE"
printf 'Env file:      %s\n' "$ENV_FILE"
printf 'Report:        %s\n' "$REPORT"

if ! cd "$REPO_DIR"; then
  bad "Cannot enter repository: $REPO_DIR"
  exit 0
fi

if ! have docker; then
  bad "docker is not installed or is not in PATH"
  exit 0
fi

if ! docker compose version >/dev/null 2>&1; then
  bad "docker compose plugin is unavailable"
  exit 0
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  bad "Missing compose file: $REPO_DIR/$COMPOSE_FILE"
  exit 0
fi

if [[ ! -f "$ENV_FILE" ]]; then
  warn "Missing env file: $REPO_DIR/$ENV_FILE"
fi

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

section "Host and repository"
run uname -a || true
run bash -c 'test -r /etc/os-release && cat /etc/os-release || true' || true
run docker --version || true
run docker compose version || true

subsection "Git state"
if have git && git rev-parse --show-toplevel >/dev/null 2>&1; then
  run git status --short --branch || true
  run git log -1 --date=iso --format='commit=%H%ncommit_date=%ad%nsubject=%s' || true
else
  warn "Not inside a Git repository"
fi

subsection "Relevant environment values"
if [[ -f "$ENV_FILE" ]]; then
  grep -E '^(AWS_ENDPOINT_URL|AWS_REGION|AWS_DEFAULT_REGION|AWS_EC2_METADATA_DISABLED|OLLAMA_URL|LLM_ENDPOINT|EMBEDDING_ENDPOINT|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|http_proxy|https_proxy|all_proxy|no_proxy)=' \
    "$ENV_FILE" 2>/dev/null | redact_url_credentials || true
else
  echo '(env file unavailable)'
fi

section "Compose configuration and state"
subsection "Configuration validation"
if "${COMPOSE[@]}" config --quiet; then
  ok "Compose configuration parses"
else
  bad "Compose configuration does not parse"
fi

subsection "Services"
run "${COMPOSE[@]}" config --services || true

subsection "Images"
run "${COMPOSE[@]}" config --images || true

subsection "Container state"
run "${COMPOSE[@]}" ps -a || true

subsection "Compose projects"
run docker compose ls || true

MOTO_CID="$(container_id_for_service moto)"
INIT_CID="$(container_id_for_service local-init)"
OLLAMA_CHECK_CID="$(container_id_for_service ollama-check)"
FRONTEND_CID="$(container_id_for_service frontend)"
REFERENCE_CID="$(first_nonempty "$MOTO_CID" "$INIT_CID" "$OLLAMA_CHECK_CID" "$FRONTEND_CID" || true)"

NETWORK_NAME=""
if [[ -n "$REFERENCE_CID" ]]; then
  NETWORK_NAME="$(
    docker inspect \
      --format '{{range $name, $cfg := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
      "$REFERENCE_CID" 2>/dev/null | head -n1
  )"
fi

if [[ -z "$NETWORK_NAME" ]] && docker network inspect cognimoss-local_default >/dev/null 2>&1; then
  NETWORK_NAME="cognimoss-local_default"
fi

NETWORK_ID=""
NETWORK_GATEWAY=""
BRIDGE_NAME=""
NETWORK_SUBNET=""

if [[ -n "$NETWORK_NAME" ]]; then
  NETWORK_ID="$(docker network inspect -f '{{.Id}}' "$NETWORK_NAME" 2>/dev/null || true)"
  NETWORK_GATEWAY="$(
    docker network inspect \
      -f '{{if .IPAM.Config}}{{(index .IPAM.Config 0).Gateway}}{{end}}' \
      "$NETWORK_NAME" 2>/dev/null || true
  )"
  NETWORK_SUBNET="$(
    docker network inspect \
      -f '{{if .IPAM.Config}}{{(index .IPAM.Config 0).Subnet}}{{end}}' \
      "$NETWORK_NAME" 2>/dev/null || true
  )"
  BRIDGE_NAME="$(
    docker network inspect \
      -f '{{index .Options "com.docker.network.bridge.name"}}' \
      "$NETWORK_NAME" 2>/dev/null || true
  )"
  if [[ -z "$BRIDGE_NAME" && -n "$NETWORK_ID" ]]; then
    BRIDGE_NAME="br-${NETWORK_ID:0:12}"
  fi
fi

section "Docker network wiring"
printf 'Reference container: %s\n' "${REFERENCE_CID:-<none>}"
printf 'Compose network:     %s\n' "${NETWORK_NAME:-<not found>}"
printf 'Network ID:          %s\n' "${NETWORK_ID:-<not found>}"
printf 'Bridge interface:    %s\n' "${BRIDGE_NAME:-<not found>}"
printf 'Network subnet:      %s\n' "${NETWORK_SUBNET:-<not found>}"
printf 'Network gateway:     %s\n' "${NETWORK_GATEWAY:-<not found>}"

if [[ -n "$NETWORK_NAME" ]]; then
  run docker network inspect "$NETWORK_NAME" || true
else
  bad "Could not determine the Compose network"
fi

subsection "Host interfaces and routes"
run ip -br address || true
run ip route show || true

if [[ -n "$BRIDGE_NAME" ]]; then
  if ip link show "$BRIDGE_NAME" >/dev/null 2>&1; then
    ok "Host bridge interface exists: $BRIDGE_NAME"
    run ip -details address show dev "$BRIDGE_NAME" || true
  else
    bad "Expected bridge interface does not exist: $BRIDGE_NAME"
  fi
fi

subsection "Docker daemon facts"
run docker info --format 'Server={{.ServerVersion}} Rootless={{json .SecurityOptions}} Driver={{.Driver}}' || true

if [[ -r /etc/docker/daemon.json ]]; then
  run cat /etc/docker/daemon.json || true
else
  echo '/etc/docker/daemon.json is absent or unreadable'
fi

section "Ollama host service"
if have systemctl; then
  run systemctl is-active ollama || true
  run systemctl show ollama \
    -p ActiveState \
    -p SubState \
    -p MainPID \
    -p ExecMainStatus \
    --no-pager || true
  run journalctl -u ollama --no-pager -n 100 || true
fi

subsection "Listening sockets"
if have ss; then
  run_shell "ss -ltnp 2>/dev/null | grep -E 'State|:11434\\b' || true"
fi

HOST_OLLAMA_OK=0
subsection "Host -> Ollama API"
if have curl; then
  HOST_OLLAMA_TMP="$(mktemp)"
  if curl -fsS \
      --connect-timeout 3 \
      --max-time 10 \
      http://127.0.0.1:11434/api/tags \
      >"$HOST_OLLAMA_TMP" 2>&1; then
    HOST_OLLAMA_OK=1
    ok "Host can reach http://127.0.0.1:11434/api/tags"
    head -c 1000 "$HOST_OLLAMA_TMP" || true
    printf '\n'
  else
    bad "Host cannot reach http://127.0.0.1:11434/api/tags"
    cat "$HOST_OLLAMA_TMP" || true
  fi
  rm -f "$HOST_OLLAMA_TMP"
else
  warn "curl is unavailable on the host"
fi

section "Exact ollama-check service environment"
if [[ -n "$OLLAMA_CHECK_CID" ]]; then
  run docker inspect "$OLLAMA_CHECK_CID" \
    --format 'name={{.Name}} status={{.State.Status}} exit={{.State.ExitCode}} started={{.State.StartedAt}} finished={{.State.FinishedAt}} error={{json .State.Error}} networks={{json .NetworkSettings.Networks}} extra_hosts={{json .HostConfig.ExtraHosts}}' || true
else
  warn "No existing ollama-check container; transient probes will still run"
fi

subsection "Environment, hosts, DNS, and route inside ollama-check"
"${COMPOSE[@]}" run --rm --no-deps \
  --entrypoint sh \
  ollama-check -lc '
    env | sort | grep -E "^(OLLAMA_URL|LLM_ENDPOINT|EMBEDDING_ENDPOINT|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|http_proxy|https_proxy|all_proxy|no_proxy)=" || true
    echo
    echo "/etc/hosts:"
    cat /etc/hosts
    echo
    echo "/etc/resolv.conf:"
    cat /etc/resolv.conf
    echo
    echo "/proc/net/route:"
    cat /proc/net/route
  ' || warn "Could not inspect ollama-check runtime environment"

COMPOSE_CONFIGURED_OLLAMA_OK=0
COMPOSE_HOSTNAME_OLLAMA_OK=0
COMPOSE_GATEWAY_OLLAMA_OK=0

subsection "ollama-check -> configured OLLAMA_URL"
if "${COMPOSE[@]}" run --rm --no-deps \
    --entrypoint sh \
    ollama-check -lc '
      set -u
      echo "OLLAMA_URL=${OLLAMA_URL:-<unset>}"
      test -n "${OLLAMA_URL:-}"
      case "$OLLAMA_URL" in
        */api/tags) url="$OLLAMA_URL" ;;
        *) url="${OLLAMA_URL%/}/api/tags" ;;
      esac
      echo "Testing: $url"
      curl -sv --connect-timeout 3 --max-time 10 "$url" -o /tmp/body
      rc=$?
      echo
      echo "curl_exit=$rc"
      if [ -f /tmp/body ]; then
        echo "body_prefix:"
        head -c 1000 /tmp/body
        echo
      fi
      exit "$rc"
    '; then
  COMPOSE_CONFIGURED_OLLAMA_OK=1
  ok "ollama-check can reach its configured OLLAMA_URL"
else
  bad "ollama-check cannot reach its configured OLLAMA_URL"
fi

subsection "ollama-check -> host.docker.internal:11434"
if "${COMPOSE[@]}" run --rm --no-deps \
    --entrypoint sh \
    ollama-check -lc '
      echo "host mapping:"
      grep host.docker.internal /etc/hosts || true
      echo
      curl -sv --connect-timeout 3 --max-time 10 \
        http://host.docker.internal:11434/api/tags \
        -o /tmp/body
      rc=$?
      echo
      echo "curl_exit=$rc"
      if [ -f /tmp/body ]; then
        echo "body_prefix:"
        head -c 1000 /tmp/body
        echo
      fi
      exit "$rc"
    '; then
  COMPOSE_HOSTNAME_OLLAMA_OK=1
  ok "Compose container can reach host.docker.internal:11434"
else
  bad "Compose container cannot reach host.docker.internal:11434"
fi

if [[ -n "$NETWORK_GATEWAY" ]]; then
  subsection "ollama-check -> direct Compose gateway ${NETWORK_GATEWAY}:11434"
  if "${COMPOSE[@]}" run --rm --no-deps \
      -e "PROBE_GATEWAY=$NETWORK_GATEWAY" \
      --entrypoint sh \
      ollama-check -lc '
        echo "Gateway: $PROBE_GATEWAY"
        curl -sv --connect-timeout 3 --max-time 10 \
          "http://${PROBE_GATEWAY}:11434/api/tags" \
          -o /tmp/body
        rc=$?
        echo
        echo "curl_exit=$rc"
        if [ -f /tmp/body ]; then
          echo "body_prefix:"
          head -c 1000 /tmp/body
          echo
        fi
        exit "$rc"
      '; then
    COMPOSE_GATEWAY_OLLAMA_OK=1
    ok "Compose container can reach direct gateway ${NETWORK_GATEWAY}:11434"
  else
    bad "Compose container cannot reach direct gateway ${NETWORK_GATEWAY}:11434"
  fi
else
  warn "No Compose gateway found; direct gateway Ollama test skipped"
fi

section "Moto and local-init wiring"
MOTO_HOST_OK=0
MOTO_CONTAINER_OK=0
BOTO_STS_OK=0

subsection "Moto container state"
if [[ -n "$MOTO_CID" ]]; then
  run docker inspect "$MOTO_CID" \
    --format 'name={{.Name}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} networks={{json .NetworkSettings.Networks}}' || true
else
  warn "Moto container is not present"
fi

subsection "Moto logs"
run "${COMPOSE[@]}" logs --no-color --tail=100 moto || true

subsection "Host -> published Moto endpoint"
if have curl; then
  MOTO_HOST_TMP="$(mktemp)"
  if curl -fsS \
      --connect-timeout 3 \
      --max-time 10 \
      http://127.0.0.1:5000/moto-api/ \
      >"$MOTO_HOST_TMP" 2>&1; then
    MOTO_HOST_OK=1
    ok "Host can reach Moto at 127.0.0.1:5000"
    head -c 500 "$MOTO_HOST_TMP" || true
    printf '\n'
  else
    bad "Host cannot reach Moto at 127.0.0.1:5000"
    cat "$MOTO_HOST_TMP" || true
  fi
  rm -f "$MOTO_HOST_TMP"
fi

subsection "local-init -> moto DNS, TCP, and HTTP"
if "${COMPOSE[@]}" run --rm --no-deps \
    --entrypoint python \
    local-init -u - <<'PY'
import http.client
import os
import socket
import sys

host = "moto"
port = 5000

print("AWS_ENDPOINT_URL =", os.getenv("AWS_ENDPOINT_URL"))
print("proxy variables:")
for key in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
):
    print(f"  {key}={os.getenv(key)!r}")

try:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    print("DNS:", addresses)
except Exception as exc:
    print("DNS FAIL:", type(exc).__name__, exc)
    sys.exit(11)

try:
    with socket.create_connection((host, port), timeout=4):
        print("TCP: OK")
except Exception as exc:
    print("TCP FAIL:", type(exc).__name__, exc)
    sys.exit(12)

try:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/moto-api/")
    response = conn.getresponse()
    body = response.read(500)
    print("HTTP:", response.status, response.reason)
    print("BODY:", body)
    conn.close()
    if not 200 <= response.status < 400:
        sys.exit(13)
except Exception as exc:
    print("HTTP FAIL:", type(exc).__name__, exc)
    sys.exit(14)
PY
then
  MOTO_CONTAINER_OK=1
  ok "local-init can resolve and reach moto:5000"
else
  bad "local-init cannot resolve or reach moto:5000"
fi

subsection "Boto3 STS -> Moto with explicit local credentials"
if "${COMPOSE[@]}" run --rm --no-deps \
    -e AWS_ACCESS_KEY_ID=test \
    -e AWS_SECRET_ACCESS_KEY=test \
    -e AWS_EC2_METADATA_DISABLED=true \
    -e NO_PROXY=moto,localhost,127.0.0.1,host.docker.internal \
    -e no_proxy=moto,localhost,127.0.0.1,host.docker.internal \
    --entrypoint python \
    local-init -u - <<'PY'
import os
import sys
import boto3
from botocore.config import Config

endpoint = os.getenv("AWS_ENDPOINT_URL", "http://moto:5000")
region = os.getenv("AWS_REGION", "us-east-1")

print("endpoint =", endpoint)
print("region   =", region)

try:
    client = boto3.client(
        "sts",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(
            connect_timeout=3,
            read_timeout=5,
            retries={"max_attempts": 0},
        ),
    )
    print(client.get_caller_identity())
except Exception as exc:
    print("BOTO3 FAIL:", type(exc).__name__, exc)
    sys.exit(21)
PY
then
  BOTO_STS_OK=1
  ok "Boto3 STS can reach Moto"
else
  bad "Boto3 STS cannot reach Moto"
fi

section "Firewall and packet-filter evidence"
NIX_CONFIG="${NIX_CONFIG:-$HOME/projects/nix_dev/configs/agent_config.nix}"
printf 'Expected Nix config: %s\n' "$NIX_CONFIG"

if [[ -r "$NIX_CONFIG" ]]; then
  run_shell "grep -nE -B3 -A8 'services\\.ollama|networking\\.firewall|allowedTCPPorts|trustedInterfaces|docker0|br-|11434|5000' \"${NIX_CONFIG}\" || true"
else
  warn "Cannot read $NIX_CONFIG"
fi

SUDO=()
if [[ $EUID -eq 0 ]]; then
  SUDO=()
elif sudo -n true >/dev/null 2>&1; then
  SUDO=(sudo -n)
else
  warn "No cached sudo authorization. Run 'sudo -v' before this script to capture firewall rules."
fi

if [[ $EUID -eq 0 || ${#SUDO[@]} -gt 0 ]]; then
  if have nft; then
    subsection "Relevant nftables rules"
    "${SUDO[@]}" nft -a list ruleset 2>&1 | \
      grep -nE -B8 -A12 '11434|5000|docker0|br-[[:alnum:]]+|br-cognimoss|cognimoss|iifname|oifname|docker' | \
      head -n 1200 || true
  fi

  if have iptables; then
    subsection "iptables INPUT"
    "${SUDO[@]}" iptables -S INPUT 2>&1 || true

    subsection "iptables FORWARD"
    "${SUDO[@]}" iptables -S FORWARD 2>&1 || true

    subsection "iptables NAT rules"
    "${SUDO[@]}" iptables -t nat -S 2>&1 | \
      grep -E 'DOCKER|11434|5000|br-|docker0' || true
  fi
fi

section "Recent service logs"
for service in moto local-init ollama-check frontend agent-worker cognee-indexer debug-worker; do
  subsection "$service"
  "${COMPOSE[@]}" logs --no-color --tail=200 "$service" 2>&1 || true
done

section "Focused container state"
for cid in "$MOTO_CID" "$INIT_CID" "$OLLAMA_CHECK_CID" "$FRONTEND_CID"; do
  [[ -n "$cid" ]] || continue
  run docker inspect "$cid" \
    --format 'name={{.Name}} image={{.Config.Image}} status={{.State.Status}} running={{.State.Running}} exit={{.State.ExitCode}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} error={{json .State.Error}} oom={{.State.OOMKilled}} networks={{json .NetworkSettings.Networks}}' || true
done

section "Automated interpretation"
printf 'Host Ollama:                    %s\n' "$HOST_OLLAMA_OK"
printf 'Configured OLLAMA_URL:          %s\n' "$COMPOSE_CONFIGURED_OLLAMA_OK"
printf 'host.docker.internal path:      %s\n' "$COMPOSE_HOSTNAME_OLLAMA_OK"
printf 'Direct Compose gateway path:    %s\n' "$COMPOSE_GATEWAY_OLLAMA_OK"
printf 'Host Moto:                      %s\n' "$MOTO_HOST_OK"
printf 'local-init -> moto:             %s\n' "$MOTO_CONTAINER_OK"
printf 'Boto3 STS -> Moto:              %s\n' "$BOTO_STS_OK"

if (( HOST_OLLAMA_OK == 0 )); then
  cat <<'TEXT'

LIKELY ROOT CAUSE:
Ollama itself is not reachable on the host. Inspect the systemd status,
journal, and listening socket above.
TEXT
elif (( COMPOSE_GATEWAY_OLLAMA_OK == 0 )); then
  cat <<'TEXT'

LIKELY ROOT CAUSE:
Docker-bridge-to-host traffic is blocked, or Ollama is not listening on the
bridge-facing host address. Focus on the active bridge name, network gateway,
and nftables INPUT rules/counters.
TEXT
elif (( COMPOSE_HOSTNAME_OLLAMA_OK == 0 )); then
  cat <<'TEXT'

LIKELY ROOT CAUSE:
host.docker.internal resolves to an unusable host-gateway address. The direct
Compose gateway works, so the firewall and Ollama listener are probably good.
Compare /etc/hosts inside ollama-check with the network gateway above.
TEXT
elif (( COMPOSE_CONFIGURED_OLLAMA_OK == 0 )); then
  cat <<'TEXT'

LIKELY ROOT CAUSE:
OLLAMA_URL is wrong, stale, malformed, or affected by proxy settings.
host.docker.internal works independently.
TEXT
else
  cat <<'TEXT'

OLLAMA WIRING PASSES:
The host API, special hostname, direct gateway, and configured URL all work.
The startup blocker is elsewhere; inspect ollama-check exit state and logs.
TEXT
fi

if (( MOTO_CONTAINER_OK == 0 )); then
  cat <<'TEXT'

SECONDARY ISSUE:
local-init cannot reach moto:5000.
TEXT
elif (( BOTO_STS_OK == 0 )); then
  cat <<'TEXT'

SECONDARY ISSUE:
Raw Moto networking works, but Boto3 STS fails. Proxy variables or AWS SDK
configuration are the leading suspects.
TEXT
fi

section "Report summary"
printf 'Checks passed:  %d\n' "$PASS"
printf 'Checks failed:  %d\n' "$FAIL"
printf 'Warnings:       %d\n' "$WARN"
printf 'Finished:       %s\n' "$(date --iso-8601=seconds 2>/dev/null || date)"
printf 'Report written: %s\n' "$REPORT"

exit 0
