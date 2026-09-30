#!/bin/bash
# Refresh the per-UE KPM headers used to address E2SM-RC controls at a UE.
# Runs the KPM gate xApp for a few seconds while both UEs carry a little load,
# parses the per-UE (ran_ue_id, amf_ue_ngap_id, guami) out of the indications,
# and writes $HW_RUNTIME_DIR/{ue1,ue2}-hdr.env by attach order.
#
# NOTE ON MAPPING: attach order (ran_ue_id) is only a guess, and a wrong guess
# addresses the wrong physical UE.  With --confirm-hosts the mapping is taken
# from the core instead: the AMF's UE table gives IMSI -> amfUeNgapId, each UE
# machine's own profile gives its IMSI, and the two are joined here.  No IMSI is
# stored in this repository or in env.sh; each one is read live, over ssh, from
# the machine that owns it.  Headers are re-derived because a UE re-attach
# changes its amfUeNgapId, and a re-attach can also swap the ran_ue_id order.
#
# --from-live reads the running KPM gate's own JSONL instead of starting a
# second gate xApp.  Starting a second one is xApp churn: it has crashed the
# near-RT RIC (exit 139) and dropped both E2 associations, which then costs a
# gNB restart and a re-pin.  Prefer --from-live whenever the gate is running.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
FROM_LIVE=""
CONFIRM_HOSTS=""
for arg in "$@"; do
  case "$arg" in
    --from-live) FROM_LIVE=1 ;;
    --confirm-hosts) CONFIRM_HOSTS=1 ;;
    *) echo "usage: $0 [--from-live] [--confirm-hosts]" >&2; exit 2 ;;
  esac
done
[ -f "$HERE/env.sh" ] && source "$HERE/env.sh" || source "$HERE/env.sh.example"

: "${HW_KPM_XAPP:?set HW_KPM_XAPP}" "${HW_XAPP_CONF:?set HW_XAPP_CONF}"
: "${HW_WITNESS:?set HW_WITNESS}" "${HW_RUNTIME_DIR:?set HW_RUNTIME_DIR}"
: "${HW_GNB1_NB_ID:?set HW_GNB1_NB_ID}" "${HW_TOPOLOGY:?set HW_TOPOLOGY}"
INV_SHA="${HW_INVENTORY_SHA:-$(cat "$HW_RUNTIME_DIR/inv_sha.txt" 2>/dev/null)}"

EPOCH=$(python3 -c "import json;w=json.load(open('$HW_WITNESS'));print(next((c['connectionEpoch'] for c in w['connections'] if c['globalE2NodeId']['nbId']==$HW_GNB1_NB_ID and c['active']),''))" 2>/dev/null)
[ -z "$EPOCH" ] && { echo "FATAL: no active epoch for nbId=$HW_GNB1_NB_ID"; exit 1; }
echo "gNB1 epoch=$EPOCH"

if [ -n "$FROM_LIVE" ]; then
  LIVE_JSONL="${HW_CAMPAIGN5_KPM_JSONL:-$HW_LIVE_ARTIFACT_ROOT/a1-live-kpm.jsonl}"
  [ -f "$LIVE_JSONL" ] || { echo "FATAL: no live KPM JSONL at $LIVE_JSONL"; exit 1; }
  SRC="$HW_RUNTIME_DIR/kpm_hdr.jsonl"
  tail -n 400 "$LIVE_JSONL" > "$SRC"
  echo "reading headers from the running gate: $LIVE_JSONL"
else
  rm -f "$HW_RUNTIME_DIR/kpm_hdr.jsonl"
  SRC="$HW_RUNTIME_DIR/kpm_hdr.jsonl"
  KPM_GATE_TOPOLOGY="$HW_TOPOLOGY" KPM_GATE_CONNECTION_EPOCHS="$EPOCH,2" \
    KPM_GATE_INVENTORY_SHA256="$INV_SHA" KPM_GATE_SNSSAIS="${HW_SNSSAIS:-1,222:00007b}" \
    KPM_GATE_JSONL="$SRC" KPM_GATE_SECONDS=16 \
    timeout 22 "$HW_KPM_XAPP" -c "$HW_XAPP_CONF" -p "$HW_RUNTIME_DIR/sm/" \
    > "$HW_RUNTIME_DIR/kpm_hdr.log" 2>&1 &
  sleep 14
fi

python3 - "$SRC" "$HW_RUNTIME_DIR" "$HW_GNB1_NB_ID" <<'PY'
import json, sys
path, outdir, nb = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    lines = open(path).read().splitlines()
except FileNotFoundError:
    print("FATAL: no KPM jsonl produced"); sys.exit(2)
# Only the NEWEST indication that carries UEs. A UE that re-attaches gets a new
# amf_ue_ngap_id, and a window of older records still holds the retired one; a
# frequency vote across the window therefore mixes generations and can address a
# UE that no longer exists.
ues = []
for line in reversed(lines):
    try: x = json.loads(line)
    except Exception: continue
    if x.get("event") != "kpm_indication" or x.get("nb_id") != nb: continue
    found = [ue for ue in (x.get("ues") or [])
             if ue.get("ue_id_type") == "gNB" and ue.get("has_ran_ue_id")]
    if found:
        ues = sorted(found, key=lambda u: u["ran_ue_id"])[:2]
        break
print("gNB1 UE count:", len(ues))
for name, u in zip(("ue1", "ue2"), ues):
    g = u["guami"]
    open(f"{outdir}/{name}-hdr.env", "w").write(
        f'RC_HEADER_RRC_UE_ID={u["ran_ue_id"]}\n'
        f'RC_HEADER_AMF_UE_NGAP_ID={u["amf_ue_ngap_id"]}\n'
        f'RC_UE_GUAMI_MCC={g["mcc"]}\nRC_UE_GUAMI_MNC={g["mnc"]}\n'
        f'RC_UE_GUAMI_MNC_LEN={g["mnc_digit_len"]}\n'
        f'RC_UE_AMF_REGION_ID={g["amf_region_id"]}\n'
        f'RC_UE_AMF_SET_ID={g["amf_set_id"]}\nRC_UE_AMF_POINTER={g["amf_pointer"]}\n')
    print(f"  {name}: ran={u['ran_ue_id']} amf={u['amf_ue_ngap_id']} -> {name}-hdr.env")
if len(ues) < 2:
    print("WARNING: fewer than two UEs seen; confirm both are attached and loaded")
PY
echo "headers written to $HW_RUNTIME_DIR/{ue1,ue2}-hdr.env"

if [ -z "$CONFIRM_HOSTS" ]; then
  echo "NEXT: --confirm-hosts joins these to the physical hosts through the core."
  exit 0
fi

# Join through the core: IMSI -> amfUeNgapId from the AMF, IMSI -> host from each
# machine's own profile.  Rewrites the header files when attach order disagreed.
amf_table=$(docker logs --tail 400 "${HW_AMF_CONTAINER:-oai-amf}" 2>&1 \
  | grep -oE '\| +[0-9]{15} +\|[0-9]* *\| +0x[0-9A-Fa-f]+ +\| +0x[0-9A-Fa-f]+' | tail -8)
[ -n "$amf_table" ] || { echo "WARNING: no AMF UE table; header<->host mapping unconfirmed"; exit 0; }

declare -A host_of_amf=()
for host_alias in "${HW_UE1_HOST:-ue1}" "${HW_UE2_HOST:-ue2}" "${HW_UE3_HOST:-ue3}"; do
  imsi=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$host_alias" \
    "grep -hoE 'imsi *= *\"[0-9]+\"' ~/ai-ran-stage/runtime/phase-b/nr-ue.conf 2>/dev/null \
     | grep -oE '[0-9]+' | head -1" 2>/dev/null)
  [ -n "$imsi" ] || continue
  amf=$(printf '%s\n' "$amf_table" | grep -F " $imsi " | grep -oE '0x[0-9A-Fa-f]+$' | tail -1)
  [ -n "$amf" ] || continue
  host_of_amf[$((amf))]="$host_alias"
done

for name in ue1 ue2; do
  f="$HW_RUNTIME_DIR/$name-hdr.env"
  [ -f "$f" ] || continue
  a=$(grep RC_HEADER_AMF_UE_NGAP_ID "$f" | cut -d= -f2)
  owner="${host_of_amf[$a]:-}"
  if [ -z "$owner" ]; then
    echo "  $name (amf=$a): host UNCONFIRMED"
  elif [ "$owner" = "${!name:-$name}" ] || [ "$owner" = "$name" ]; then
    echo "  $name (amf=$a): confirmed as $owner"
  else
    echo "  $name (amf=$a): the core says this is $owner, not $name"
  fi
done
