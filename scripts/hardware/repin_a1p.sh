#!/bin/bash
# Re-pin the A1-P inventory, KPM gate, and Assurance binding after gNB restart.
# This is intentionally the only mutating hardware-lane command in this file.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/env.sh" ] && source "$HERE/env.sh" || source "$HERE/env.sh.example"
DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=true; shift; fi
[ "$#" -eq 0 ] || { echo "usage: $0 [--dry-run]" >&2; exit 2; }
# Keep the retired spelling only as a one-release read fallback for existing
# operator env.sh files. All paths below use the current public name.
HW_LIVE_ARTIFACT_ROOT="${HW_LIVE_ARTIFACT_ROOT:-${LOWER_LIVE:-}}"
: "${HW_LIVE_ARTIFACT_ROOT:?HW_LIVE_ARTIFACT_ROOT must be set in env.sh}"
: "${HW_ASSURANCE_BINDING:?HW_ASSURANCE_BINDING must be set in env.sh}"

RIC=oran-aic-nearrt-ric
PRODUCER=oran-aic-a1p-producer
GATE=oran-aic-kpm-gate
say() { printf '%s\n' "$*"; }
run() { say "+ $*"; "$@"; }
plan() { say "PLAN: $*"; }

# The current containers, not repository defaults, are the source of the image,
# command, mount and KPM environment.  The small JSON handoff exposes no secret
# values and fails before an action if any required inspected field is absent.
inspect_json="$(mktemp)"
trap 'rm -f "$inspect_json"' EXIT
say "+ docker inspect $RIC $PRODUCER $GATE"
docker inspect "$RIC" "$PRODUCER" "$GATE" > "$inspect_json"
eval "$(python3 - "$inspect_json" <<'PY'
import json, shlex, sys
containers={x['Name'].lstrip('/'): x for x in json.load(open(sys.argv[1]))}
for name in ('oran-aic-nearrt-ric','oran-aic-a1p-producer','oran-aic-kpm-gate'):
    if name not in containers: raise SystemExit('missing inspected container '+name)
ric, prod, gate=(containers[x] for x in ('oran-aic-nearrt-ric','oran-aic-a1p-producer','oran-aic-kpm-gate'))
def env(c): return dict(x.split('=',1) for x in c['Config'].get('Env',[]) if '=' in x)
def flag(c, key):
    cmd=c['Config'].get('Cmd') or []
    if key not in cmd: raise SystemExit('%s lacks %s' % (c['Name'],key))
    return cmd[cmd.index(key)+1]
def out(key, value):
    if not value: raise SystemExit('empty inspected '+key)
    print('export %s=%s' % (key,shlex.quote(str(value))))
renv, penv, genv=env(ric),env(prod),env(gate)
out('WITNESS', renv.get('FLEXRIC_E2_CONNECTION_WITNESS_PATH'))
out('PRODUCER_IMAGE', prod['Config'].get('Image'))
out('PRODUCER_PYTHONPATH', penv.get('PYTHONPATH'))
out('CONTRACT_ROOT', flag(prod,'--contract-root'))
out('HANDOFF_SHA256', flag(prod,'--expected-handoff-sha256'))
out('LIVE_CONFIG', flag(prod,'--live-xapp-config'))
out('GATE_IMAGE', gate['Config'].get('Image'))
out('GATE_CONFIG', (gate['Config'].get('Cmd') or [])[2] if len(gate['Config'].get('Cmd') or []) > 2 else '')
out('GATE_SM_DIR', (gate['Config'].get('Cmd') or [])[4] if len(gate['Config'].get('Cmd') or []) > 4 else '')
out('GATE_BINARY', (gate['Config'].get('Cmd') or [])[0] if gate['Config'].get('Cmd') else '')
out('KPM_TOPOLOGY', genv.get('KPM_GATE_TOPOLOGY'))
# 2026-09-23: copied verbatim, the conductor's 14400 s made the gate exit every four
# hours and a board running then lost its KPM identity.  Never carry a lifetime shorter
# than a week forward; a longer one is kept.
out('KPM_SECONDS', str(max(int(genv.get('KPM_GATE_SECONDS') or 0), 604800)))
out('KPM_SNSSAIS_INSPECTED', genv.get('KPM_GATE_SNSSAIS') or '')
out('GATE_NETWORK', gate.get('HostConfig',{}).get('NetworkMode'))
out('GATE_BINDS_JSON', json.dumps(gate.get('HostConfig',{}).get('Binds') or []))
out('PRODUCER_BINDS_JSON', json.dumps(prod.get('HostConfig',{}).get('Binds') or []))
PY
)"

epoch_json="$(mktemp)"
trap 'rm -f "$inspect_json" "$epoch_json"' EXIT
say "+ docker exec $RIC cat $WITNESS"
docker exec "$RIC" cat "$WITNESS" > "$epoch_json"
eval "$(python3 - "$epoch_json" <<'PY'
import json,shlex,sys
w=json.load(open(sys.argv[1])); epochs={c['globalE2NodeId']['nbId']:c['connectionEpoch'] for c in w['connections'] if c.get('active')}
for nb in (3584,2816):
    if nb not in epochs: raise SystemExit('active witness lacks nbId %s' % nb)
print('export GNB1_EPOCH='+shlex.quote(str(epochs[3584])))
print('export GNB2_EPOCH='+shlex.quote(str(epochs[2816])))
PY
)"
EPOCH_TAG="epoch${GNB1_EPOCH}-${GNB2_EPOCH}"
RUN_ID="${EPOCH_TAG}-$(date -u +%Y%m%dT%H%M%S%N)"
CAPTURE_DIR="$HW_LIVE_ARTIFACT_ROOT/e2-setup-capture-$EPOCH_TAG"
PROBE_JSONL="$HW_LIVE_ARTIFACT_ROOT/probe-$EPOCH_TAG.jsonl"
INVENTORY="$HW_LIVE_ARTIFACT_ROOT/e2-capability-inventory.json"
KPM_JSONL="$HW_LIVE_ARTIFACT_ROOT/a1-live-kpm.jsonl"
KPM_ROTATED_BACKUP="$KPM_JSONL.pre-rotate-$RUN_ID"
BUNDLE_ROOT="$(dirname "$(dirname "$(dirname "$CONTRACT_ROOT")")")"
SLOTS="$BUNDLE_ROOT/runtime/e2-inventory-slots.json"
RELEASE="$BUNDLE_ROOT/manifests/backend-release-manifest.json"

if "$DRY_RUN"; then
  plan "copy current witness artifacts to $CAPTURE_DIR"
  plan "run e2_inventory_probe_xapp on inspected host network using $KPM_TOPOLOGY"
  plan "materialize inventory (staged) in inspected producer image as root"
  plan "refresh capability manifest via standard_release.py finalize into manifests/refresh-$EPOCH_TAG (no live mutation)"
  plan "backup immediate live state as *.before-$RUN_ID, rotate the KPM JSONL before recreating the gate, and restart the producer after the fresh stream starts"
  plan "publish refreshed capability/inventory and integration values, re-pin all moved binding source digests, then publish the binding last"
  exit 0
fi

say "1/5 Copying current E2 setup artifacts for $EPOCH_TAG"
run mkdir -p "$CAPTURE_DIR"
for epoch in "$GNB1_EPOCH" "$GNB2_EPOCH"; do
  for suffix in capture.json e2-setup.aper ran-function-2.aper ran-function-3.aper; do
    run docker cp "$RIC:${WITNESS}.artifacts/epoch-${epoch}-${suffix}" "$CAPTURE_DIR/"
  done
done

say "2/5 Running one inspected-release E2 inventory probe"
PROBE_BINARY="$(dirname "$GATE_BINARY")/e2_inventory_probe_xapp"
# the gate mounts service-models at /sm inside its container; the probe runs on the host, so map it back
# The running gate may bind its SM directory at an absolute host path.
# Resolve the inspected paths through the actual mounts, including file binds.
eval "$(python3 - "$inspect_json" "$GATE_SM_DIR" "$GATE_BINARY" <<'PYMOUNTS'
import json, pathlib, shlex, sys
container = next(c for c in json.load(open(sys.argv[1]))
                 if c['Name'].lstrip('/') == 'oran-aic-kpm-gate')
def host_path(value):
    value = pathlib.PurePosixPath(value)
    for mount in sorted(container['Mounts'], key=lambda m: len(m['Destination']), reverse=True):
        try:
            suffix = value.relative_to(mount['Destination'])
        except ValueError:
            continue
        return str(pathlib.Path(mount['Source']) / suffix)
    raise SystemExit('no inspected bind mount covers ' + str(value))
print('SM_HOST=' + shlex.quote(host_path(sys.argv[2])))
print('PROBE_HOST=' + shlex.quote(str(pathlib.Path(host_path(sys.argv[3])).with_name('e2_inventory_probe_xapp'))))
PYMOUNTS
)"
[ -d "$SM_HOST" ] || { echo "inspected service-models directory is absent" >&2; exit 1; }
[ -x "$PROBE_HOST" ] || { echo "inventory probe beside the running gate binary is absent" >&2; exit 1; }
mapfile -t gate_mount_args < <(python3 - "$GATE_BINDS_JSON" <<'PY'
import json,shlex,sys
for bind in json.loads(sys.argv[1]): print('-v'); print(bind)
PY
)
# The probe is a FlexRIC xApp whose -p buffer overflows on a long host path; run it
# inside the gate image with the same mounts so /sm/ (short) resolves, on the host network.
run docker run --rm --network "$GATE_NETWORK" "${gate_mount_args[@]}" \
  -v "$SM_HOST:/sm:ro" -v "$PROBE_HOST:$PROBE_BINARY:ro" \
  -e "INVENTORY_PROBE_TOPOLOGY=$KPM_TOPOLOGY" -e "INVENTORY_PROBE_JSONL=$PROBE_JSONL" \
  -e "INVENTORY_PROBE_DISCOVERY_SECONDS=${HW_INVENTORY_PROBE_DISCOVERY_SECONDS:-3}" \
  "$GATE_IMAGE" "$PROBE_BINARY" -c "$GATE_CONFIG" -p /sm/
python3 - "$PROBE_JSONL" <<'PY'
import json,sys
records=[json.loads(x) for x in open(sys.argv[1], encoding='utf-8') if x.strip()]
if not records or records[-1].get('event') != 'e2_inventory_probe_complete' or records[-1].get('ok') is not True:
    raise SystemExit('e2_inventory_probe_complete ok=true not observed')
PY


say "3/5 Materializing capability inventory (staged) inside inspected producer image as root"
# Reuse inspected producer mounts, making only the live artifact root writable for the new
# inventory.  This avoids guessing image paths while preserving its import tree.
mapfile -t producer_mount_args < <(python3 - "$PRODUCER_BINDS_JSON" "$HW_LIVE_ARTIFACT_ROOT" <<'PY'
import json,shlex,sys
for bind in json.loads(sys.argv[1]):
    src,dst,*mode=bind.split(':')
    suffix=':rw' if src == sys.argv[2] else (':'+mode[0] if mode else '')
    print('-v'); print(src+':'+dst+suffix)
PY
)
INVENTORY_STAGED="$INVENTORY.staging-$EPOCH_TAG"
run docker run --rm --network host --user 0 -e "PYTHONPATH=$PRODUCER_PYTHONPATH" \
  "${producer_mount_args[@]}" "$PRODUCER_IMAGE" python3 -m xapp.a1p_producer.e2_inventory \
  --contract-root "$CONTRACT_ROOT" --handoff-sha256 "$HANDOFF_SHA256" --release "$RELEASE" \
  --artifact-root "$HW_LIVE_ARTIFACT_ROOT/release-root" --capture-dir "$CAPTURE_DIR" --probe-jsonl "$PROBE_JSONL" \
  --witness "$WITNESS" --slots "$SLOTS" --output "$INVENTORY_STAGED"
python3 - "$INVENTORY_STAGED" <<'PY'
import json,sys
doc=json.load(open(sys.argv[1], encoding='utf-8'))
if doc.get('status') != 'READY':
    raise SystemExit('materialized inventory status is not READY')
if 'staging-' in json.dumps(doc):
    raise SystemExit('materialized inventory embeds its staging path; cannot publish by rename')
PY
NEW_SHA="$(sha256sum "$INVENTORY_STAGED" | awk '{print $1}')"

say "4/5 Refreshing the capability manifest (requiredRanFunctions) from the current captures"
# The producer pins each node's KPM/RC definition digests in the capability manifest; a gNB
# software change (new E2SM definitions) must be re-finalized or the producer refuses startup.
# finalize writes only into manifests/refresh-<tag>/ (no live pin is touched here).
OTA_EVIDENCE_REL="$(python3 - "$RELEASE" <<'PY'
import json,sys
doc=json.load(open(sys.argv[1],encoding='utf-8'))
paths=[p['path'] for p in doc.get('profiles',[]) if p.get('name')=='OTA-READBACK-EVIDENCE']
if len(paths)!=1: raise SystemExit('release manifest lacks exactly one OTA-READBACK-EVIDENCE profile')
print(paths[0])
PY
)"
REFRESH_REL="manifests/refresh-$EPOCH_TAG"
REFRESH_DIR="$BUNDLE_ROOT/$REFRESH_REL"
FINALIZER="$BUNDLE_ROOT/source/backend/scripts/release/standard_release.py"
run docker run --rm --network host --user 0 -e "PYTHONPATH=$PRODUCER_PYTHONPATH" \
  -v "$BUNDLE_ROOT:$BUNDLE_ROOT:rw" -v "$HW_LIVE_ARTIFACT_ROOT:$HW_LIVE_ARTIFACT_ROOT:rw" -v /run/ai-ran:/run/ai-ran:ro \
  "$PRODUCER_IMAGE" python3 "$FINALIZER" finalize \
  --bundle-root "$BUNDLE_ROOT" --contract-root "$CONTRACT_ROOT" --handoff-sha256 "$HANDOFF_SHA256" \
  --ota-evidence "$OTA_EVIDENCE_REL" --capture-dir "$CAPTURE_DIR" --probe-jsonl "$PROBE_JSONL" \
  --witness "$WITNESS" --live-witness "$WITNESS" --telemetry-jsonl "$KPM_JSONL" \
  --release-output "$REFRESH_REL/backend-release-manifest.json" \
  --inventory-output "$REFRESH_REL/e2-capability-inventory.json" \
  --capability-output "$REFRESH_REL/backend-capability-manifest.json" \
  --live-config-output "$REFRESH_REL/live-flexric.json" \
  --report-output "$REFRESH_REL/lower-release-readiness.json"
run docker run --rm --user 0 -v "$BUNDLE_ROOT:$BUNDLE_ROOT:rw" "$PRODUCER_IMAGE" chown -R "$(id -u):$(id -g)" "$REFRESH_DIR"
cmp -s "$RELEASE" "$REFRESH_DIR/backend-release-manifest.json" || {
  echo "refreshed release manifest differs from the pinned $RELEASE; refusing to re-pin" >&2; exit 1; }
CAPABILITY="$REFRESH_DIR/backend-capability-manifest.json"
CAP_SHA="$(sha256sum "$CAPABILITY" | awk '{print $1}')"
# The producer verifies path/sha256, path/byteSha256, artifact-manifest, and
# canonical decoded-definition references.  The actual loader is the only
# faithful preflight for that recursive contract; invoke it against the staged
# config below before any live file is published.
ARTIFACT_ROOT="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['artifactRoot'])" "$LIVE_CONFIG")"
python3 - "$ARTIFACT_ROOT" "$CAPABILITY" "$INVENTORY_STAGED" <<'PY'
import hashlib,json,os,sys
root=sys.argv[1]; bad=[]; count=0
def walk(v, where):
    global count
    if isinstance(v, dict):
        if isinstance(v.get('path'), str) and isinstance(v.get('sha256'), str):
            p=v['path']; full=p if os.path.isabs(p) else os.path.join(root,p); count+=1
            try: digest=hashlib.sha256(open(full,'rb').read()).hexdigest()
            except OSError: bad.append(where+': missing '+full); return
            if digest!=v['sha256']: bad.append(where+': sha256 mismatch '+full)
        for k,x in v.items(): walk(x, where+'.'+k)
    elif isinstance(v, list):
        for i,x in enumerate(v): walk(x, '%s[%d]' % (where,i))
for label,path in (('capability',sys.argv[2]),('inventory',sys.argv[3])):
    walk(json.load(open(path,encoding='utf-8')), label)
if bad: raise SystemExit('artifact references not resolvable under %s:\n  %s' % (root,'\n  '.join(bad)))
print('verified %d artifact references under %s' % (count, root))
PY

say "5/5 Backing up immediate live state, publishing pins, verifying the gate, then publishing rApp digests and binding"
DEPLOYMENT_DIR="$(dirname "$HW_LIVE_ARTIFACT_ROOT")/deployment"
RAPP_CAPABILITY="$DEPLOYMENT_DIR/capability.json"
RAPP_INVENTORY="$DEPLOYMENT_DIR/inventory.json"
INTEGRATION_VALUES="$DEPLOYMENT_DIR/integration-values.json"
for path in "$INVENTORY" "$LIVE_CONFIG" "$KPM_JSONL" "$HW_ASSURANCE_BINDING" \
            "$RAPP_CAPABILITY" "$RAPP_INVENTORY" "$INTEGRATION_VALUES"; do
  [ -e "$path" ] && run cp -p "$path" "$path.before-$RUN_ID"
done
LIVE_STAGED="$LIVE_CONFIG.staging-$RUN_ID"
BINDING_STAGED="$HW_ASSURANCE_BINDING.staging-$RUN_ID"
CAPABILITY_STAGED="$RAPP_CAPABILITY.staging-$RUN_ID"
RAPP_INVENTORY_STAGED="$RAPP_INVENTORY.staging-$RUN_ID"
INTEGRATION_STAGED="$INTEGRATION_VALUES.staging-$RUN_ID"
cp -p "$LIVE_CONFIG" "$LIVE_STAGED"
cp -p "$HW_ASSURANCE_BINDING" "$BINDING_STAGED"
cp -p "$CAPABILITY" "$CAPABILITY_STAGED"
cp -p "$INVENTORY_STAGED" "$RAPP_INVENTORY_STAGED"
cp -p "$INTEGRATION_VALUES" "$INTEGRATION_STAGED"
python3 - "$LIVE_STAGED" "$NEW_SHA" "$BINDING_STAGED" "$GNB1_EPOCH" "$GNB2_EPOCH" "$CAPABILITY" "$CAP_SHA" <<'PY'
import json,sys
live,sha,binding,g1,g2,cap,cap_sha=sys.argv[1:]
with open(live, encoding='utf-8') as f: doc=json.load(f)
doc['e2CapabilityInventory']['sha256']=sha
doc['capabilityManifest']={'path':cap,'sha256':cap_sha}
with open(live,'w',encoding='utf-8') as f: json.dump(doc,f,indent=2); f.write('\n')
with open(binding, encoding='utf-8') as f: doc=json.load(f)
epochs=doc['kpm']['expectedEpochs']
for node in list(epochs):
    if 'nb=0000003584' in node: epochs[node]=int(g1)
    elif 'nb=0000002816' in node: epochs[node]=int(g2)
    else: raise SystemExit('binding has unexpected E2 node '+node)
with open(binding,'w',encoding='utf-8') as f: json.dump(doc,f,indent=2); f.write('\n')
PY

# This loader mirrors _verify_manifest_artifacts() exactly, including byteSha256,
# artifactManifest*, and canonical decoded definition checks.
# Precheck copy of the staged live config: the inventory is still staged (not yet published), so the
# loader must be pointed at the staged inventory path; the published config keeps the live path.
LIVE_PRECHECK="$LIVE_STAGED.precheck"
python3 - "$LIVE_STAGED" "$LIVE_PRECHECK" "$INVENTORY_STAGED" <<'PY2'
import json,sys
src,dst,inv=sys.argv[1:]
doc=json.load(open(src,encoding="utf-8")); doc["e2CapabilityInventory"]["path"]=inv
json.dump(doc,open(dst,"w",encoding="utf-8"),indent=2)
PY2
run docker run --rm --network host --pid host --user 0 -e "PYTHONPATH=$PRODUCER_PYTHONPATH" \
  "${producer_mount_args[@]}" "$PRODUCER_IMAGE" python3 -c \
  "from xapp.a1p_producer.artifacts import ContractArtifacts; from xapp.a1p_producer.live_flexric import LiveFlexRicExecutionPort; artifacts=ContractArtifacts.load('$CONTRACT_ROOT', expected_handoff_sha256='$HANDOFF_SHA256'); LiveFlexRicExecutionPort.from_file(artifacts, '$LIVE_PRECHECK'); print('staged live FlexRIC loader READY')"
rm -f "$LIVE_PRECHECK"

python3 - "$INTEGRATION_STAGED" "$CAPABILITY_STAGED" "$RAPP_INVENTORY_STAGED" <<'PY'
import hashlib,json,sys
integration,capability,inventory=sys.argv[1:]
with open(integration,encoding='utf-8') as f: doc=json.load(f)
values=doc.get('values')
if not isinstance(values,dict): raise SystemExit('integration-values has no values object')
values['backend.capabilityManifestSha256']=hashlib.sha256(open(capability,'rb').read()).hexdigest()
values['backend.e2CapabilityInventorySha256']=hashlib.sha256(open(inventory,'rb').read()).hexdigest()
with open(integration,'w',encoding='utf-8') as f: json.dump(doc,f,indent=2); f.write('\n')
PY

MUTATED=false
NEW_GATE=false
OLD_GATE="${GATE}.before-${RUN_ID}"
PUBLISHED=false
rollback() {
  say "ROLLBACK: restoring immediate pre-mutation pins and prior KPM gate"
  if [ "$NEW_GATE" = true ]; then docker stop -t 15 "$GATE" >/dev/null 2>&1 || true; docker rm "$GATE" >/dev/null 2>&1 || true; fi
  if docker inspect "$OLD_GATE" >/dev/null 2>&1; then docker rename "$OLD_GATE" "$GATE" >/dev/null 2>&1 || true; docker start "$GATE" >/dev/null 2>&1 || true; fi
  # A rotation changes the stream inode.  Restore the original file, rather
  # than copying over the new one, so the producer sees the prior stream again.
  if [ -e "$KPM_ROTATED_BACKUP" ]; then rm -f "$KPM_JSONL"; mv -f "$KPM_ROTATED_BACKUP" "$KPM_JSONL"; fi
  for path in "$INVENTORY" "$LIVE_CONFIG" "$RAPP_CAPABILITY" "$RAPP_INVENTORY" "$INTEGRATION_VALUES" "$HW_ASSURANCE_BINDING"; do
    cp -p "$path.before-$RUN_ID" "$path" 2>/dev/null || true
  done
  docker restart "$PRODUCER" >/dev/null 2>&1 || true
}
cleanup() { code=$?; trap - EXIT ERR INT TERM; if [ "$MUTATED" = true ] && [ "$PUBLISHED" != true ]; then rollback; fi; exit "$code"; }
trap cleanup EXIT
trap 'exit 1' ERR
trap 'exit 130' INT TERM
MUTATED=true
run mv -f "$INVENTORY_STAGED" "$INVENTORY"
run mv -f "$LIVE_STAGED" "$LIVE_CONFIG"
START_TS="$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)"  # RFC3339 Z, nanosecond precision
run docker restart "$PRODUCER"
producer_ok=false
for _ in $(seq 1 20); do
  logs="$(docker logs --since "$START_TS" "$PRODUCER" 2>&1 || true)"
  if grep -qi 'refusing' <<<"$logs"; then
    grep -i 'refusing' <<<"$logs" | tail -3 >&2; echo 'producer refused startup after re-pin' >&2; false
  fi
  if grep -qi 'xApp mode is live FlexRIC' <<<"$logs" && [ "$(docker inspect -f '{{.State.Running}}' "$PRODUCER")" = true ]; then
    producer_ok=true; break
  fi
  sleep 2
done
[ "$producer_ok" = true ] || { echo 'producer did not confirm live FlexRIC after restart' >&2; false; }

mapfile -t gate_mount_args < <(python3 - "$GATE_BINDS_JSON" <<'PY'
import json,shlex,sys
for bind in json.loads(sys.argv[1]): print('-v'); print(bind)
PY
)
# Which S-NSSAIs the gate asks the E2 nodes to report UEs for.  The gate's own
# default is sst 1 only, so a UE on any other slice is simply absent from the
# stream: gNB2 reported "No UE matches the condition criteria" while a UE was
# attached and in-sync on sst 222.  Keep the inspected value when the running
# gate already had one, else take the lab's list.
KPM_SNSSAIS="${KPM_SNSSAIS_INSPECTED:-}"
[ -n "$KPM_SNSSAIS" ] || KPM_SNSSAIS="${HW_SNSSAIS:-1}"

# Stop the gate gracefully first: an abrupt xApp disconnect has segfaulted the near-RT RIC.
run docker stop -t 15 "$GATE" >/dev/null 2>&1 || true
run docker rename "$GATE" "$OLD_GATE"
# The committed KPM validator inspects the stream head.  Preserve the previous
# stream under this run's unique backup and let the replacement gate create a
# new JSONL, so its first records carry only the newly pinned epochs.
run mv "$KPM_JSONL" "$KPM_ROTATED_BACKUP"
run docker run -d --name "$GATE" --network "$GATE_NETWORK" "${gate_mount_args[@]}" \
  -e "KPM_GATE_TOPOLOGY=$KPM_TOPOLOGY" -e "KPM_GATE_SECONDS=$KPM_SECONDS" -e "KPM_GATE_JSONL=$KPM_JSONL" \
  -e "KPM_GATE_CONNECTION_EPOCHS=$GNB1_EPOCH,$GNB2_EPOCH" -e "KPM_GATE_INVENTORY_SHA256=$NEW_SHA" \
  -e "KPM_GATE_SNSSAIS=$KPM_SNSSAIS" \
  "$GATE_IMAGE" "$GATE_BINARY" -c "$GATE_CONFIG" -p "$GATE_SM_DIR"
NEW_GATE=true
gate_ok=false
for _ in $(seq 1 30); do
  if [ "$(docker inspect -f '{{.State.Running}}' "$GATE" 2>/dev/null)" != true ]; then
    docker logs --tail 5 "$GATE" >&2 || true; echo 'KPM gate exited' >&2; false
  fi
  if python3 - "$KPM_JSONL" "$GNB1_EPOCH" "$GNB2_EPOCH" "$NEW_SHA" <<'PY'
import json,sys
path,g1,g2,sha=sys.argv[1],int(sys.argv[2]),int(sys.argv[3]),sys.argv[4]
seen=set()
try:
    with open(path,encoding='utf-8') as f:
        for i,line in enumerate(f):
            # Match the committed wiring validator: only the stream head is
            # admissible after a re-pin, never historical records appended later.
            if i >= 16: break
            if not line.strip(): continue
            try: x=json.loads(line)
            except ValueError: continue
            if x.get('event')!='kpm_indication' or x.get('inventory_sha256')!=sha: continue
            if x.get('nb_id')==3584 and x.get('connection_epoch')==g1: seen.add(3584)
            if x.get('nb_id')==2816 and x.get('connection_epoch')==g2: seen.add(2816)
except FileNotFoundError:
    pass
sys.exit(0 if seen=={3584,2816} else 1)
PY
  then gate_ok=true; break; fi
  sleep 2
done
[ "$gate_ok" = true ] || { echo "KPM gate did not report both nodes at epochs $GNB1_EPOCH,$GNB2_EPOCH with inventory $NEW_SHA" >&2; false; }
# Rotation creates a new inode.  Restart the producer only after the fresh gate
# has written its head, otherwise its stream cursor can retain the old inode.
FRESH_STREAM_TS="$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)"
run docker restart "$PRODUCER"
fresh_producer_ok=false
for _ in $(seq 1 20); do
  logs="$(docker logs --since "$FRESH_STREAM_TS" "$PRODUCER" 2>&1 || true)"
  if grep -qi 'refusing' <<<"$logs"; then echo 'producer refused startup after KPM rotation' >&2; false; fi
  if grep -qi 'xApp mode is live FlexRIC' <<<"$logs" && [ "$(docker inspect -f '{{.State.Running}}' "$PRODUCER")" = true ]; then fresh_producer_ok=true; break; fi
  sleep 2
done
[ "$fresh_producer_ok" = true ] || { echo 'producer did not confirm live FlexRIC after KPM rotation' >&2; false; }
# The rApp files are published only after the producer and replacement gate are
# healthy.  Re-pin every binding source whose bytes moved, then prove the staged
# binding loads before its final atomic replacement.
run mv -f "$CAPABILITY_STAGED" "$RAPP_CAPABILITY"
run mv -f "$RAPP_INVENTORY_STAGED" "$RAPP_INVENTORY"
run mv -f "$INTEGRATION_STAGED" "$INTEGRATION_VALUES"
python3 - "$BINDING_STAGED" <<'PY'
import hashlib,json,sys
path=sys.argv[1]
with open(path,encoding='utf-8') as f: doc=json.load(f)
for source in doc.get('sources',[]):
    candidate=source.get('path')
    if isinstance(candidate,str):
        try: source['sha256']=hashlib.sha256(open(candidate,'rb').read()).hexdigest()
        except OSError: pass
with open(path,'w',encoding='utf-8') as f: json.dump(doc,f,indent=2); f.write('\n')
PY
PYTHONPATH="$HERE/../..${PYTHONPATH:+:$PYTHONPATH}" run python3 - "$BINDING_STAGED" <<'PY'
import sys
from assurance.contracts.live_binding import load_assurance_live_binding
load_assurance_live_binding(sys.argv[1])
print('staged Assurance binding source digests verified')
PY
run mv -f "$BINDING_STAGED" "$HW_ASSURANCE_BINDING"
PUBLISHED=true
run docker rm "$OLD_GATE" >/dev/null
say "Re-pin complete: epochs $GNB1_EPOCH,$GNB2_EPOCH, inventory $NEW_SHA, capability $CAP_SHA"
