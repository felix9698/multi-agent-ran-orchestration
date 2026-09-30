#!/bin/bash
# Fire one E2SM-RC Style-2 control at the bound node over O-RAN (our_rc_xapp).
# NEVER telnet: this is xApp -> near-RT RIC -> E2 (E2SM-RC CONTROL) -> gNB E2 agent.
#
# Usage:
#   RC_ACTION=102 RC_CAP_MAX_DL_PRBS=6  bash scripts/hardware/fire_xapp.sh ue1
#   RC_ACTION=103 RC_PF_WEIGHT=8        bash scripts/hardware/fire_xapp.sh ue1
#   RC_ACTION=101 RC_MCS_MIN=0 RC_MCS_MAX=28  bash scripts/hardware/fire_xapp.sh ue1
#   RC_ACTION=104 RC_TX_ATTEN_DB=6      bash scripts/hardware/fire_xapp.sh ue1
#   RC_ACTION=6   RC_SLICE_SST=1 RC_SLICE_MIN_RATIO=1 RC_SLICE_MAX_RATIO=100 ... ue1
#
# The operator form reads the live E2 epoch from the RIC witness.  The producer
# form accepts an invocation-specific, worker-owned header file which includes
# the fixed node and exact epoch captured with the KPM identity.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/env.sh" ] && source "$HERE/env.sh" || source "$HERE/env.sh.example"

: "${HW_XAPP_BIN:?set HW_XAPP_BIN}" "${HW_XAPP_CONF:?set HW_XAPP_CONF}"
: "${HW_RUNTIME_DIR:?set HW_RUNTIME_DIR}"

authoritative=0
if [ "${1:-}" = "--header-file" ]; then
  [ "$#" -eq 2 ] || { echo "usage: $0 --header-file <worker-header.env>" >&2; exit 2; }
  hdr="$2"
  ue="worker-header:$(basename "$hdr")"
  authoritative=1
else
  [ "$#" -eq 1 ] || { echo "usage: $0 <ue-tag: ue1|ue2>" >&2; exit 2; }
  ue="$1"
  hdr="$HW_RUNTIME_DIR/${ue}-hdr.env"
fi
[ -f "$hdr" ] || { echo "no header file $hdr; run refresh_headers.sh first"; exit 1; }

if [ "$authoritative" -eq 1 ]; then
  # Require both fields in this invocation's header, not in inherited env.
  unset RC_SOURCE_CONNECTION_EPOCH RC_SOURCE_NB_ID
fi
set -a; source "$hdr"; set +a
: "${RC_HEADER_RRC_UE_ID:?header lacks RC_HEADER_RRC_UE_ID}"
: "${RC_HEADER_AMF_UE_NGAP_ID:?header lacks RC_HEADER_AMF_UE_NGAP_ID}"
: "${RC_UE_GUAMI_MCC:?header lacks RC_UE_GUAMI_MCC}"
: "${RC_UE_GUAMI_MNC:?header lacks RC_UE_GUAMI_MNC}"
: "${RC_UE_GUAMI_MNC_LEN:?header lacks RC_UE_GUAMI_MNC_LEN}"
: "${RC_UE_AMF_REGION_ID:?header lacks RC_UE_AMF_REGION_ID}"
: "${RC_UE_AMF_SET_ID:?header lacks RC_UE_AMF_SET_ID}"
: "${RC_UE_AMF_POINTER:?header lacks RC_UE_AMF_POINTER}"

if [ "$authoritative" -eq 1 ]; then
  : "${RC_SOURCE_CONNECTION_EPOCH:?authoritative header lacks RC_SOURCE_CONNECTION_EPOCH}"
  : "${RC_SOURCE_NB_ID:?authoritative header lacks RC_SOURCE_NB_ID}"
  EPOCH="$RC_SOURCE_CONNECTION_EPOCH"
  NODE="$RC_SOURCE_NB_ID"
  [[ "$NODE" =~ ^[0-9]+$ && "$EPOCH" =~ ^[0-9]+$ ]] || {
    echo "FATAL: authoritative node/epoch must be decimal integers" >&2; exit 9;
  }
else
  : "${HW_WITNESS:?set HW_WITNESS}" "${HW_GNB1_NB_ID:?set HW_GNB1_NB_ID}"
  NODE="$HW_GNB1_NB_ID"
  EPOCH=$(python3 -c "import json;w=json.load(open('$HW_WITNESS'));print(next(c['connectionEpoch'] for c in w['connections'] if c['globalE2NodeId']['nbId']==$HW_GNB1_NB_ID and c['active']))" 2>/dev/null) || EPOCH=""
fi
[ -z "$EPOCH" ] && { echo "FATAL: no active epoch for nbId=$NODE in witness" >&2; exit 9; }

export RC_SOURCE_NGRAN_TYPE=2 RC_SOURCE_MCC=208 RC_SOURCE_MNC=95 RC_SOURCE_MNC_LEN=2
export RC_SOURCE_NB_ID="$NODE" RC_SOURCE_NB_UNUSED_BITS=0
export RC_SOURCE_HAS_CU_DU_ID=0 RC_SOURCE_CU_DU_ID=0 RC_SOURCE_CONNECTION_EPOCH="$EPOCH"

# Action 104 carries RAN param 233 (Target gNB ID) and the xApp treats it as
# mandatory -- fired without it, it returns no control outcome at all.  That is
# why the power axis never once settled before 2026-09-18.  The target is the
# node this invocation's header already named, so derive it rather than ask
# every caller to repeat it; a caller that names a *different* node would
# attenuate a cell we did not admit and drop every UE on it, so refuse that.
if [ "${RC_ACTION:-}" = "104" ]; then
  [ "${RC_TARGET_GNB_ID:-$NODE}" = "$NODE" ] || {
    echo "FATAL: RC_TARGET_GNB_ID=$RC_TARGET_GNB_ID is not this header's node $NODE" >&2; exit 9;
  }
  export RC_TARGET_GNB_ID="$NODE"
fi

echo "firing RC_ACTION=${RC_ACTION:-102} at $ue (epoch=$EPOCH) over E2SM-RC ..."
timeout 18 "$HW_XAPP_BIN" -c "$HW_XAPP_CONF" -p "$HW_RUNTIME_DIR/sm/" 2>&1 \
  | grep -iE "action=|success=|CONTROL|ACK|reject|CAPABILITY|FATAL|required|must be" | head -40  # FATAL/required: without them a fire that died on a missing mandatory
  # parameter printed nothing but the config line, and readback stayed at the
  # baseline -- which reads exactly like "the axis does not work".  That is how
  # RC_TARGET_GNB_ID stayed unnoticed from 2026-09-16 to 2026-09-18.
