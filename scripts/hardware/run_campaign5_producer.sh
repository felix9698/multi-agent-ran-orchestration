#!/bin/bash
# Operator-lane launcher for the in-repo Campaign-5 A1-P live worker.
# All deployment identities, ports and secret/TLS references come from env.sh.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
ENV_FILE="$HERE/env.sh"
[ -f "$ENV_FILE" ] || {
  echo "FATAL: $ENV_FILE is required; the example is not a live binding" >&2
  exit 2
}
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${HW_CAMPAIGN5_LISTEN_HOST:?set in scripts/hardware/env.sh}"
: "${HW_CAMPAIGN5_PORT:?set in scripts/hardware/env.sh}"
: "${HW_CAMPAIGN5_TLS_CERT_REF:?set file reference in scripts/hardware/env.sh}"
: "${HW_CAMPAIGN5_TLS_KEY_REF:?set file reference in scripts/hardware/env.sh}"
: "${HW_CAMPAIGN5_CLIENT_CA_REF:?set file reference in scripts/hardware/env.sh}"
: "${HW_CAMPAIGN5_BEARER_TOKEN_REF:?set file reference in scripts/hardware/env.sh}"
: "${HW_RUNTIME_DIR:?set in scripts/hardware/env.sh}"
# One-release read fallback for existing env.sh files that have not migrated.
HW_LIVE_ARTIFACT_ROOT="${HW_LIVE_ARTIFACT_ROOT:-${LOWER_LIVE:-}}"
: "${HW_LIVE_ARTIFACT_ROOT:?set in scripts/hardware/env.sh}"

ref_path() {
  case "$1" in
    file://*) printf '%s\n' "${1#file://}" ;;
    *) echo "FATAL: secret/TLS reference must use file://" >&2; return 2 ;;
  esac
}

TLS_CERT="$(ref_path "$HW_CAMPAIGN5_TLS_CERT_REF")"
TLS_KEY="$(ref_path "$HW_CAMPAIGN5_TLS_KEY_REF")"
CLIENT_CA="$(ref_path "$HW_CAMPAIGN5_CLIENT_CA_REF")"
TOKEN_FILE="$(ref_path "$HW_CAMPAIGN5_BEARER_TOKEN_REF")"
KPM_JSONL="${HW_CAMPAIGN5_KPM_JSONL:-$HW_LIVE_ARTIFACT_ROOT/a1-live-kpm.jsonl}"
LEDGER="${HW_CAMPAIGN5_LEDGER:-$HW_RUNTIME_DIR/campaign5-live-worker.json}"
PROBE_SECONDS="${HW_CAMPAIGN5_GATE_PROBE_SECONDS:-2}"

# Optional operator-owned JSON list of {cellId, nbId, ledgerPath}. This does
# not discover or enable another cell unless the operator supplies the map.
if [ -n "${HW_CAMPAIGN5_CELL_BINDINGS:-}" ]; then
  [ -f "$HW_CAMPAIGN5_CELL_BINDINGS" ] || {
    echo "FATAL: explicit cell-bindings map is absent" >&2; exit 3;
  }
  BINDING_ARGS=(--cell-bindings "$HW_CAMPAIGN5_CELL_BINDINGS")
else
  : "${HW_CAMPAIGN5_CELL_ID:?set the fixed target cell in scripts/hardware/env.sh}"
  : "${HW_GNB1_NB_ID:?set in scripts/hardware/env.sh}"
  BINDING_ARGS=(--ledger "$LEDGER" --cell-id "$HW_CAMPAIGN5_CELL_ID" --nb-id "$HW_GNB1_NB_ID")
fi

for required_file in "$TLS_CERT" "$TLS_KEY" "$CLIENT_CA" "$TOKEN_FILE" \
                     "$KPM_JSONL" "$HERE/fire_xapp.sh"; do
  [ -f "$required_file" ] || {
    echo "FATAL: required operator-lane file is absent: $required_file" >&2
    exit 3
  }
done

# A non-empty but dead JSONL is not a live gate.  Require append progress in a
# short operator-configurable window before enabling any writer.
before="$(stat -c '%s:%Y' "$KPM_JSONL")"
sleep "$PROBE_SECONDS"
after="$(stat -c '%s:%Y' "$KPM_JSONL")"
[ "$after" != "$before" ] || {
  echo "FATAL: KPM gate is not writing $KPM_JSONL; live worker remains disabled" >&2
  exit 4
}

cd "$ROOT"
exec python3 -m oran.campaign5.producer \
  --listen-host "$HW_CAMPAIGN5_LISTEN_HOST" \
  --port "$HW_CAMPAIGN5_PORT" \
  --tls-cert "$TLS_CERT" \
  --tls-key "$TLS_KEY" \
  --client-ca "$CLIENT_CA" \
  --secret-file "$TOKEN_FILE" \
  --live-xapp \
  --kpm-jsonl "$KPM_JSONL" \
  --header-dir "$HW_RUNTIME_DIR" \
  --fire-xapp "$HERE/fire_xapp.sh" \
  "${BINDING_ARGS[@]}" \
  --freshness-seconds "${HW_CAMPAIGN5_KPM_MAX_AGE_SECONDS:-5}" \
  --control-deadline-seconds "${HW_CAMPAIGN5_CONTROL_DEADLINE_SECONDS:-18}" \
  --expiry-poll-seconds "${HW_CAMPAIGN5_EXPIRY_POLL_SECONDS:-1}"
