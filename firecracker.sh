#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: firecracker.sh <action> <payload.json> [timeout]

Actions: create, start, stop, reboot, delete, status, recover, console
EOF
}

fail() {
  echo "{\"status\":\"error\",\"error\":\"$1\"}"
  exit 1
}

[[ $# -lt 2 ]] && { usage; fail "Missing action or payload file"; }

ACTION="$1"
PAYLOAD_FILE="$2"
TIMEOUT="${3:-${FC_AGENT_TIMEOUT:-30}}"

[[ -r "$PAYLOAD_FILE" ]] || fail "Payload file not found or unreadable"
if ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]]; then
  fail "Timeout must be an integer"
fi

RAW_PAYLOAD="$(<"$PAYLOAD_FILE")"

mapfile -t FIELDS < <(
  jq -r '[
      (.vm_name // ."cloudstack.vm.details".name // ."cloudstack.vm.details".uuid // "vm"),
      (.host_url // .externaldetails.host.url // ""),
      (.host_port // .externaldetails.host.port // .externaldetails.host.agent_port // 8000),
      (.host_username // .externaldetails.host.username // .externaldetails.host.user // .externaldetails.host.login // .username // ""),
      (.host_password // .externaldetails.host.password // .externaldetails.host.pass // .password // ""),
      (.host_token // .externaldetails.host.token // .externaldetails.host.agent_token // ""),
      ((.skip_ssl_verification // .host_skip_ssl_verification // .externaldetails.host.skip_ssl_verification // "false") | ascii_downcase),
      (.ca_bundle // .externaldetails.host.ca_bundle // .externaldetails.host.ca_cert // ""),
      (.client_cert // .externaldetails.host.client_cert // ""),
      (.client_key // .externaldetails.host.client_key // "")
    ] | @tsv' <<<"$RAW_PAYLOAD"
)

IFS=$'\t' read -r VM_NAME HOST_URL HOST_PORT HOST_USERNAME HOST_PASSWORD HOST_TOKEN SKIP_VERIFY CA_BUNDLE CLIENT_CERT CLIENT_KEY <<<"${FIELDS[0]}"

[[ -z "$HOST_URL" ]] && fail "host_url or externaldetails.host.url is required"
if [[ "$VM_NAME" =~ [^A-Za-z0-9-] ]]; then
  fail "Invalid VM name '$VM_NAME'. Only alphanumeric characters and dashes are allowed."
fi

if [[ "$HOST_URL" != *"://"* ]]; then
  HOST_URL="http://$HOST_URL"
  HAS_PORT=false
else
  authority="${HOST_URL#*://}"
  authority="${authority%%/*}"
  if [[ "$authority" == *:* ]]; then
    HAS_PORT=true
  else
    HAS_PORT=false
  fi
fi

BASE="${HOST_URL%/}"
if [[ $HAS_PORT == false ]]; then
  BASE="${BASE}:${HOST_PORT}"
fi
API_BASE="${BASE%/}/v1"

declare -a CURL_OPTS
CURL_OPTS=(-sS -w '\n%{http_code}')

if [[ "$SKIP_VERIFY" == "true" || "$SKIP_VERIFY" == "1" ]]; then
  CURL_OPTS+=(-k)
fi
if [[ -n "$CA_BUNDLE" ]]; then
  CURL_OPTS+=(--cacert "$CA_BUNDLE")
fi
if [[ -n "$CLIENT_CERT" ]]; then
  if [[ -n "$CLIENT_KEY" ]]; then
    CURL_OPTS+=(--cert "$CLIENT_CERT" --key "$CLIENT_KEY")
  else
    CURL_OPTS+=(--cert "$CLIENT_CERT")
  fi
fi

declare -a CURL_HEADERS
CURL_HEADERS=(-H "Accept: application/json")
if [[ -n "$HOST_TOKEN" ]]; then
  CURL_HEADERS+=(-H "Authorization: Bearer $HOST_TOKEN")
fi
if [[ -n "$HOST_USERNAME" ]]; then
  CURL_OPTS+=(-u "${HOST_USERNAME}:${HOST_PASSWORD}")
fi

call_api() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  local args=("${CURL_OPTS[@]}" -X "$method" "${CURL_HEADERS[@]}")
  if [[ -n "$body" ]]; then
    args+=(-H "Content-Type: application/json" -d "$body")
  fi

  local response
  if ! response=$(curl "${args[@]}" "$API_BASE$path"); then
    fail "HTTP request failed"
  fi
  local http_code="${response##*$'\n'}"
  local body_content="${response%$'\n'$http_code}"

  if [[ "$http_code" =~ ^[45] ]]; then
    local err
    err=$(jq -r '.error // .message // @json' <<<"$body_content" 2>/dev/null || echo "$body_content")
    fail "Agent error ($http_code): $err"
  fi

  echo "$body_content"
}

payload_with_spec() {
  jq -c --argjson timeout "$TIMEOUT" '{spec: ., timeout: $timeout}' "$PAYLOAD_FILE"
}

timeout_payload() {
  jq -n --argjson timeout "$TIMEOUT" '{timeout: $timeout}'
}

case "$ACTION" in
  create)
    call_api POST "/vms" "$(payload_with_spec)"
    ;;
  start)
    call_api POST "/vms/${VM_NAME}/start" "$(payload_with_spec)"
    ;;
  stop)
    call_api POST "/vms/${VM_NAME}/stop" "$(timeout_payload)"
    ;;
  reboot)
    call_api POST "/vms/${VM_NAME}/reboot" "$(timeout_payload)"
    ;;
  delete)
    call_api DELETE "/vms/${VM_NAME}"
    ;;
  status)
    call_api GET "/vms/${VM_NAME}/status"
    ;;
  recover)
    call_api POST "/vms/${VM_NAME}/recover" "$(payload_with_spec)"
    ;;
  console)
    call_api POST "/vms/${VM_NAME}/console"
    ;;
  *)
    usage
    fail "Invalid action '$ACTION'"
    ;;
esac
