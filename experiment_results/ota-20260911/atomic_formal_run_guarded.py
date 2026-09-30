#!/usr/bin/env python3
"""One authenticated original-pilot attempt; import performs no I/O.

Run only after the operator has restored the real deployment. This script does
not deploy sources, recover radio/producer state, lower targets, or declare OTA
success. Every attempt gets new files and a new session. A timeout may require
Kernel/Gateway reconciliation; source cleanup is not control-plane rollback.
"""
import argparse
import base64
import concurrent.futures

# 2026-09-22: 판이 한 시행에서 30분을 멎었는데 이 진입점에는 스택을 뜰 수단이 없어
# 원인을 로그로 추측해야 했다.  SIGUSR1 로 전 스레드 스택을 찍어 두면 다음 정체는
# 몇 초 만에 갈린다.  keeper 의 워치독이 거두기 직전에 이 신호를 보낸다.
try:
    import faulthandler as _fh
    import signal as _sig
    _fh.register(_sig.SIGUSR1, all_threads=True, chain=False)
except Exception:
    pass
import copy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
DIRECTORY = Path(__file__).resolve().parent
PILOT = DIRECTORY / 'pilot38-v3-select10-20260914T1530'
AUTHENTICATION_HOLD = DIRECTORY / 'live-authentication-hold.json'

PILOT_INTENTS_SHA = '00d57dbaeac15fba864119412faabc8656c4a2d2c399b797f50fa123ad97aac3'
PILOT_MANIFEST_SHA = '18bf24b2a46bdec20f1fefdf1eac72354e0e9aa45694b2c9042c8b4178569992'
SOURCE = '/tmp/aic-flow-9140cea9b49e-beaa921ad26a'
#: The host join helper lives beside this runner.  It used to point into one
#: session's /tmp scratchpad, which a reboot deletes: the runner would then refuse
#: every attempt with HOST_JOIN_HELPER_MISSING on the first unattended night.
HOST_JOIN = DIRECTORY / 'ops' / 'ue_host_map.py'
HOSTS = ('ue1', 'ue2', 'ue3')
FLOWS = {'ue1': 'ue1-data', 'ue2': 'ue2-map', 'ue3': 'ue3-incumbent'}
CELLS = (12345678, 87654321)
#: The owner preference profiles of the integrated reply section 3, named
#: exactly as agent.py::PREFERENCE_PROFILES names them.
PREFERENCE_PROFILES = ('P1', 'P2', 'P3')


class Refused(RuntimeError):
    """Only fixed, credential-free diagnostic codes belong in this exception."""


def require(condition, code):
    if not condition:
        raise Refused(code)


def raise_root_cause(errors):
    """Raise the failure that cancelled the others, not the cancellation.

    Futures read in list order reported ``SOURCE_START_CANCELLED`` from the
    first pair while the cause sat in a later one (2026-09-15 attempt 65).
    """
    errors = [error for error in errors if error is not None]
    if errors:
        raise next((error for error in errors if str(error) != 'SOURCE_START_CANCELLED'), errors[0])


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mapping_by_host(mapping):
    require(isinstance(mapping, dict) and len(mapping) == 3, 'HOST_JOIN_INCOMPLETE')
    require(set(mapping.values()) == set(HOSTS), 'HOST_JOIN_NOT_ONE_TO_ONE')
    require(all(isinstance(ue, str) and re.fullmatch(r'(?:0|[1-9][0-9]{0,12})', ue)
                and int(ue) < 2**40 for ue in mapping), 'HOST_JOIN_INVALID_AMF_ID')
    return {host: ue for ue, host in mapping.items()}


#: 핸드오프 2026-09-18 §8.1: 코드·dirty-worktree·프롬프트·스키마·카탈로그·evaluator·predictor 를
#: manifest/hash 로 고정한다.  §6 재평가 때 68판 전부가 "code version unknown" 이었던 이유는
#: 판 기록에 이것이 없었기 때문이다.  프롬프트·스키마·predictor 는 agents.py, evaluator 는 tc.py,
#: 카탈로그 생성·해시는 contracts/catalog.py, 판정·소진은 kernel/*.
_CODE_ROOTS = ('assurance', 'tools', 'oran', 'decision', 'xapp')
_PINNED_SOURCES = (
    'assurance/coordination/agents.py', 'assurance/coordination/tc.py',
    'assurance/coordination/intake.py', 'assurance/contracts/catalog.py',
    'assurance/kernel/kernel.py', 'assurance/kernel/reducer.py',
    'tools/liveconsole/agent.py', 'tools/liveconsole/build.py',
    'decision/llm_backend.py', 'orc_task/SINGLE_CALL.md',
    'experiment_results/ota-20260911/ops/run_case_v4.sh',
    'experiment_results/ota-20260911/atomic_formal_run_guarded.py')


def _code_version() -> dict:
    """이 판이 실제로 돈 코드 -- 커밋 + **모든 코드 파일의 실제 내용** 해시.

    git 을 부르지 않는다: 이 모듈의 테스트는 ``subprocess.run`` 을 막거나 가짜로 바꿔
    두므로, 여기서 부르면 가짜 CLI 에 걸린다(2026-09-19, AttemptTests 9개).  대신
    코드 디렉터리의 *.py 전부와 핵심 문서·셸을 직접 해시한다 -- 커밋 안 된 변경과
    추적 안 되는 파일까지 빠짐없이 잡는다.
    """
    head = ''
    try:
        ref = (REPO / '.git' / 'HEAD').read_text().strip()
        if ref.startswith('ref: '):
            path = REPO / '.git' / ref[5:]
            head = path.read_text().strip() if path.exists() else ''
            if not head:
                packed = REPO / '.git' / 'packed-refs'
                for line in (packed.read_text().splitlines() if packed.exists() else ()):
                    if line.endswith(' ' + ref[5:]):
                        head = line.split(' ', 1)[0]
        else:
            head = ref
    except OSError:
        pass
    files = {}
    for root in _CODE_ROOTS:
        for path in sorted((REPO / root).rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            files[str(path.relative_to(REPO))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for rel in _PINNED_SOURCES:
        path = REPO / rel
        files[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    combined = hashlib.sha256(json.dumps({'head': head, 'files': files},
                                         sort_keys=True).encode()).hexdigest()
    return {'gitHead': head, 'codeFiles': len(files),
            'sourceSha256': {rel: files[rel] for rel in _PINNED_SOURCES},
            'codeVersionSha256': combined}


def _pilot_pins(pilot):
    """The pilot directory to read and the two hashes it must have.

    ``AIC_PILOT`` lets the calibration probe supply its own intent set, but the
    pin does not come off with it: the operator has to state both hashes as
    well, so a substituted intents file is still refused unless it is exactly
    the one that was named.
    """
    named = os.environ.get('AIC_PILOT')
    if not named or pilot is not PILOT:
        return pilot, PILOT_INTENTS_SHA, PILOT_MANIFEST_SHA
    intents_sha = os.environ.get('AIC_PILOT_INTENTS_SHA') or ''
    manifest_sha = os.environ.get('AIC_PILOT_MANIFEST_SHA') or ''
    require(bool(intents_sha) and bool(manifest_sha), 'PILOT_OVERRIDE_NEEDS_BOTH_HASHES')
    return Path(named), intents_sha, manifest_sha


def pilot_answers(pilot=PILOT):
    """The answers file of the pilot actually in force, override included.

    ``--answers`` used to name ``PILOT/'answers.json'`` directly while
    ``--intents-json`` went through :func:`_pilot_pins`, so an ``AIC_PILOT``
    override changed the intents and left the shaping table behind.  v4's first
    three episodes were refused before submission with "the constraint
    'ue1-video may only sit at (I1g.r1, I1d.r1#deadline) = (0,0), (1,0), (2,0)'
    names I1d.r1#deadline, which no requirement carries" -- v3.1's per-owner
    (g,d) table, read against a domain that no longer has a ue1 deadline.  The
    hash pins still apply: :func:`_pilot_pins` refuses an override that does not
    state both of them.
    """
    resolved, _intents_sha, _manifest_sha = _pilot_pins(pilot)
    return resolved / 'answers.json'


def pilot_intents(mapping, pilot=PILOT):
    pilot, intents_sha, manifest_sha = _pilot_pins(pilot)
    require(sha(pilot/'intents.json') == intents_sha, 'PILOT_INTENTS_HASH_MISMATCH')
    require(sha(pilot/'manifest.json') == manifest_sha, 'PILOT_MANIFEST_HASH_MISMATCH')
    old_hosts = json.loads((pilot/'manifest.json').read_text())['ueHosts']
    mapping_by_host(old_hosts)
    current = mapping_by_host(mapping)
    document = json.loads((pilot/'intents.json').read_text())
    # Intents name the UE by role (its host), not by the id it holds at launch: the
    # network re-registers a UE under a new id mid-attempt, and the sitting resolves
    # the role to the current id through ueIdentityPath
    # (docs/design/ue-identity-continuity.md).  ``current`` still validates the join.
    for intent in document['intents']:
        # 셀 사업자 요구는 `ueId` 가 없고 `requirement.scope` 가 `cell@<nci>` 다.  셀 id 는
        # 판마다 바뀌지 않으므로 재매핑할 것이 없고, 여기서 KeyError 를 내면 코퍼스가
        # 통째로 거절된다.
        if intent.get('ueId') is None:
            require(bool(str((intent.get('requirement') or {}).get('scope') or '').strip()),
                    'INTENT_WITHOUT_UE_NEEDS_SCOPE')
            continue
        intent['ueId'] = old_hosts[intent['ueId']]
    return document


def check_identities(values, pinned=None):
    require(set(values) == set(HOSTS), 'TUN_IDENTITY_INCOMPLETE')
    addresses = []
    for identity in values.values():
        require(isinstance(identity, dict) and identity.get('name') == 'oaitun_ue1'
                and identity.get('up') is True and type(identity.get('ifindex')) is int
                and identity['ifindex'] > 0, 'TUN_NOT_UP_OR_IDENTITY_INVALID')
        require(isinstance(identity.get('bootId'), str)
                and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', identity['bootId']),
                'TUN_BOOT_ID_MISSING')
        try:
            address = ipaddress.IPv4Address(identity.get('ip'))
        except (ValueError, TypeError):
            raise Refused('TUN_IPV4_INVALID') from None
        require(address in ipaddress.IPv4Network('12.1.1.0/24'), 'TUN_IPV4_OUTSIDE_UE_NETWORK')
        addresses.append(address)
    require(len(set(addresses)) == 3, 'TUN_IPV4_NOT_UNIQUE')
    if pinned is not None:
        require(values == pinned, 'TUN_IDENTITY_CHANGED')
    return values


def specs_for(identities, base_port, duration_s=600):
    require(type(base_port) is int and 1024 <= base_port <= 65532, 'INVALID_PORT_RANGE')
    # The upper bound was 900 s while the sitting carried a 420 s deadline B. With B
    # removed (2026-09-16) a sitting runs until its trial budget is spent, so the sources
    # have to outlive it or the episode just dies at the listener instead of at the clock.
    # The bound stays -- a runaway duration would leave listeners behind for the next
    # attempt -- it is only raised to the same backstop the runner uses.
    require(type(duration_s) is int and 30 <= duration_s <= 7200, 'INVALID_SOURCE_DURATION')
    specs = []
    for index, host in enumerate(HOSTS):
        shared = {'host': host, 'port': base_port+index, 'flowId': FLOWS[host],
                  'ip': identities[host]['ip'], 'durationS': duration_s}
        specs.append({**shared, 'endpoint': host, 'slot': host+'-rx', 'role': 'receiver',
                      'identity': identities[host]})
        specs.append({**shared, 'endpoint': 'extdn', 'slot': host+'-tx', 'role': 'sender',
                      'identity': None})
    # I4-I6 are one response requirement per UE, so every UE needs its own echo
    # pair.  Hardcoding ue1 here is why deadlineSuccessRatio was never observed
    # for ue2 or ue3: the measurement path did not exist for them.
    for index, host in enumerate(HOSTS):
        echo = {'host': host, 'port': base_port+3+index,
                'flowId': host + '-command', 'durationS': duration_s}
        specs.extend([{**echo, 'endpoint': 'extdn', 'slot': host+'-echo-server',
                       'role': 'echo_server', 'identity': None},
                      {**echo, 'endpoint': host, 'slot': host+'-echo-client',
                       'role': 'echo_client', 'identity': identities[host]}])
    return specs


class Remote:
    def __init__(self, session, source_dir=SOURCE):
        self.session = session
        self.source_dir = source_dir
        self.code = (DIRECTORY/'guarded_source_process.py').read_text()

    def call(self, endpoint, action, *, spec=None, listener=False, timeout=10):
        require(endpoint in (*HOSTS, 'extdn'), 'UNKNOWN_ENDPOINT')
        payload = {'action': action, 'session': self.session, 'sourceDir': self.source_dir,
                   'endpoint': endpoint, 'spec': spec, 'listener': listener}
        python = ['python3', '-c', self.code]
        # docker exec does not inherit this process's environment, so the offered
        # load the helper reads (AIC_OFFERED_LOAD_MBPS) never crossed into the
        # container and every sender ran at the 1 Mbps default -- which is why the
        # initial measurement met every requirement and no search ever started.
        # Forwarded explicitly, and only this name: the load is the environment,
        # not an action axis (P0-19), and the helper keeps its own range guard.
        load = os.environ.get('AIC_OFFERED_LOAD_MBPS')
        forward = ['-e', 'AIC_OFFERED_LOAD_MBPS=' + load] if load else []
        command = (['docker', 'exec', '-i', *forward, 'oai-ext-dn', *python] if endpoint == 'extdn'
                   else ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3',
                         endpoint, shlex.join(python)])
        try:
            result = subprocess.run(command, input=json.dumps(payload), capture_output=True,
                                    text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise Refused('REMOTE_TIMEOUT:'+endpoint+':'+action) from None
        try:
            document = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise Refused('REMOTE_INVALID_REPLY:'+endpoint+':'+action) from None
        if result.returncode != 0 or document.get('ok') is not True:
            error = document.get('error', '')
            safe = error if re.fullmatch(r'[A-Z][A-Z0-9_]*(?::[a-z_]+\.py)?', error) else 'REMOTE_REFUSED'
            raise Refused(safe+':'+endpoint+':'+action)
        require(isinstance(document.get('result'), dict), 'REMOTE_RESULT_NOT_OBJECT')
        return document['result']


def kpm_dependencies(deployment, mapping=None):
    """Reuse the framework tail and attribution reader; accept no stale IDs."""
    from assurance.live.pin_to_cell_driver import KpmUeAttributionReader, LiveTiming, kpm_node_nb_id
    from tools.g3ota.composition import KpmTail, live_topology
    topology = live_topology(deployment.binding, deployment.capability)
    require(set(topology.nb_id_to_nci.values()) == set(CELLS), 'TWO_CELL_TOPOLOGY_REQUIRED')
    lines = KpmTail(deployment.kpm_jsonl_path).read_new_lines()
    now = datetime.now(timezone.utc)
    bound = LiveTiming().freshness_bound_ms
    fresh_lines, nodes = [], {}
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or row.get('event') != 'kpm_indication':
                continue
            node, nb, epoch, at = (row.get(key) for key in
                                  ('e2_node', 'nb_id', 'connection_epoch', 'recv_unix_us'))
            if not (type(nb) is int and type(epoch) is int and type(at) is int
                    and nb in topology.nb_id_to_nci and kpm_node_nb_id(node) == nb
                    and topology.expected_epochs.get(node) == epoch):
                continue
            age_ms = (now.timestamp()*1_000_000-at)/1000
            if not 0 <= age_ms <= bound:
                continue
            fresh_lines.append(line)
            if node not in nodes or at > nodes[node]['receivedUnixUs']:
                nodes[node] = {'nbId': nb, 'cell': topology.nb_id_to_nci[nb],
                               'connectionEpoch': epoch, 'receivedUnixUs': at,
                               'traceSha256': hashlib.sha256(line.strip().encode()).hexdigest()}
        except (ValueError, TypeError):
            continue
    require({item['cell'] for item in nodes.values()} == set(CELLS), 'FRESH_TWO_CELL_KPM_REQUIRED')
    result = {'checkedAt': now.isoformat(), 'freshnessBoundMs': bound, 'nodes': nodes}
    if mapping is not None:
        mapping_by_host(mapping)
        reader = KpmUeAttributionReader(read_new_lines=lambda: fresh_lines, topology=topology)
        reader.refresh()
        associations, observations = {}, {}
        for ue in mapping:
            item = reader.at_or_before(now.isoformat(), lookback_ms=bound, amf_ue_ngap_id=int(ue))
            require(item is not None, 'FRESH_UE_KPM_REQUIRED:'+ue)
            # A simultaneously fresh ID on two cells is ambiguous, not a choice
            # this launch script may silently make on behalf of the Kernel.
            matches = [obs for obs in reader.observations() if obs.amf_ue_ngap_id == int(ue)]
            require(len({(obs.serving_nci, obs.e2_node, obs.connection_epoch)
                         for obs in matches}) == 1, 'UE_KPM_ATTRIBUTION_AMBIGUOUS:'+ue)
            associations[ue] = item.serving_nci
            observations[ue] = {'amfUeNgapId': item.amf_ue_ngap_id, 'servingNci': item.serving_nci,
                                'e2Node': item.e2_node, 'connectionEpoch': item.connection_epoch,
                                'observedAt': item.observed_at, 'traceHash': item.trace_hash,
                                'guAmI': dict(item.gu_ami)}
        result.update(initialAssociation=associations, observations=observations)
    return result


def r1_dependencies(profile_path, state_dir):
    """Discovery only: never create/delete/withdraw a policy in preflight."""
    from tools.liveconsole.profile import load_live_deployment
    from tools.g3ota.composition import build_r1_policy_port, build_policy_type_discovery
    deployment = load_live_deployment(profile_path)
    kpm = kpm_dependencies(deployment)
    producer = deployment.action_producer
    require(producer is not None, 'SUPPLEMENTARY_PRODUCER_REQUIRED')
    cap = producer.for_action('ue-dl-prb-cap')
    require(cap is not None, 'PRB_CAP_POLICY_TYPE_REQUIRED')
    state_dir.mkdir(mode=0o700)
    port = build_r1_policy_port(deployment.values, state_path=state_dir/'primary.json')
    discovery = build_policy_type_discovery(port, policy_type_id=deployment.binding.r1.policy_type_id,
                                           capability_manifest=deployment.capability)
    require(deployment.binding.r1.policy_type_id in discovery['policyTypeIds'], 'PRIMARY_TYPE_NOT_DISCOVERED')
    action = build_r1_policy_port({**deployment.values, 'r1.apiRoot': producer.api_root},
                                 state_path=state_dir/'cap.json')
    action.bootstrap_info()
    action.discover_services()
    require(cap.policy_type_id in action.discover_policy_types(), 'CAP_TYPE_NOT_DISCOVERED')
    require(bool(action.get_policy_type(cap.policy_type_id)), 'CAP_TYPE_DETAIL_MISSING')
    return {'kpm': kpm, 'r1Discovery': {'primary': deployment.binding.r1.policy_type_id,
                                     'supplementary': cap.policy_type_id},
            'deploymentHashes': {str(path): sha(path) for path in
                                 (deployment.binding_path, deployment.capability_path,
                                  deployment.integration_values_path)}}


def dependency_preflight(profile_path, root):
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--dependency-check',
                             str(profile_path), str(root/'dependency-r1')],
                            capture_output=True, text=True, timeout=35, cwd=REPO)
    # Never archive raw library stderr, security references or credential values.
    try:
        document = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise Refused('DEPENDENCY_CHECK_INVALID_REPLY') from None
    require(result.returncode == 0 and document.get('ok') is True,
            'DEPENDENCY_PREFLIGHT_REFUSED:'+document.get('errorType', 'UNKNOWN'))
    return document['result']


def join_hosts(profile, destination, helper=HOST_JOIN):
    require(helper.is_file(), 'HOST_JOIN_HELPER_MISSING')
    require(not destination.exists(), 'HOST_JOIN_OUTPUT_EXISTS')
    result = subprocess.run([sys.executable, str(helper), str(profile), str(destination), *HOSTS],
                            capture_output=True, text=True, timeout=30)
    require(result.returncode == 0, 'HOST_JOIN_FAILED')
    try:
        mapping = json.loads(result.stdout)
        profile_document = json.loads(destination.read_text())
    except (ValueError, OSError):
        raise Refused('HOST_JOIN_INVALID_REPLY') from None
    mapping_by_host(mapping)
    require(profile_document['liveConsole']['ueHosts'] == mapping, 'HOST_JOIN_PROFILE_MISMATCH')
    return mapping, profile_document


def make_profile(base_document, base_path, root, mapping, specs, session):
    profile = copy.deepcopy(base_document)
    for key in ('integrationValuesPath', 'capabilityManifestPath'):
        if profile.get(key):
            profile[key] = str((base_path.parent / profile[key]).resolve())
    block = profile['liveConsole']
    for key in ('assuranceBindingPath', 'producerDatabasePath'):
        block[key] = str((base_path.parent / block[key]).resolve())
    profile['description'] = 'Guarded original-pilot attempt; no OTA-success assertion.'
    profile['runsRoot'] = str(root/'runs')
    block['r1StateDir'] = str(root/'r1-state')
    block['evidenceDir'] = str(root/'evidence')
    if block.get('actionProducer'):
        block['actionProducer']['r1StateDir'] = str(root/'action-r1-state')
    mapping_by_host(mapping)
    block['ueHosts'] = {host: host for host in HOSTS}
    block['ueIdentityPath'] = str(role_identity_path(root))
    block['flowGoodput'], block['taggedEcho'] = {}, {}
    for spec in specs:
        if spec['role'] not in ('receiver', 'echo_client'):
            continue
        is_flow = spec['role'] == 'receiver'
        block['flowGoodput' if is_flow else 'taggedEcho'][spec['host']] = {
            'sourcePath': SOURCE+('/flow_goodput.py' if is_flow else '/tagged_echo.py'),
            'logPath': f"/tmp/aic-{session}/{spec['slot']}.jsonl", 'sessionId': session,
            'flowId': spec['flowId'], 'maxAgeMs': 1500}
    profile['intentDefaults'] = []
    return profile


def prepared_board(root, ids):
    """``--prepared-tc`` for this attempt, or nothing when no board was banked.

    ``AIC_PREPARED_BOARD`` names an episode whose ``T`` and ``C`` were formed by
    an earlier preparation; its UE keys are swapped for the ones this attempt
    addresses and the result is written beside the attempt. exp_metrics.md
    section 1 permits the reuse while the defining inputs are unchanged -- the
    intents and the manifest are hash-pinned above, so they are -- and it takes
    the Target and Control calls off the live path, leaving only Trajectory.
    """
    banked = os.environ.get('AIC_PREPARED_BOARD') or ''
    if not banked or not Path(banked).is_file():
        return ()
    # execute_once.py puts only the repo on sys.path, so a bare import of a
    # sibling module raises ModuleNotFoundError and the attempt dies before the
    # CLI ever starts -- with an empty live-sitting.stdout, which hides it.
    # Load it by path, the way execute_once.py loads this runner.
    import importlib.util
    _spec = importlib.util.spec_from_file_location('prepare_tc', DIRECTORY/'prepare_tc.py')
    _prepare = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_prepare)
    rekey = _prepare.rekey

    episode = json.loads(Path(banked).read_text(encoding='utf-8'))
    was = {host: ue for ue, host in (episode.get('ueHosts') or {}).items()}
    swap = {was[host]: ids[host] for host in ids
            if was.get(host) and was[host] != ids[host]}
    out = root / 'prepared-tc.json'
    out.write_text(json.dumps({'T': rekey(episode['T'], swap),
                               'C': rekey(episode['C'], swap),
                               'preparedFrom': {'episodeId': episode.get('episodeId'),
                                                'prepMs': (episode.get('timing') or {}).get('prepMs'),
                                                'rekeyed': swap}}, indent=1), encoding='utf-8')
    return ('--prepared-tc', str(out))


def _cap_axis_flags(ids, stated):
    """``--cap-axis`` for each named host, or the formal run's three UEs.

    ``stated`` is ``host:rungs`` repeated by **semicolon** (``'ue1:6,12;ue3:5'``)
    -- the rungs themselves are comma separated, so the entries cannot be.  A
    host left out carries no cap axis, which is how the calibration probe keeps
    a UE that holds no intent entirely out of the frozen action space --
    ``_axis_specs`` builds a cap axis for every UE named by an intent *or* by a
    ``--cap-axis``, so omitting both is the only way to leave one alone.
    """
    # Unset means the formal run's three ladders.  Set-but-empty means the
    # caller wants no cap axis at all -- an axis kind is exposed for every UE
    # that names one, so this is the only way to keep a family out entirely.
    if stated is None:
        return tuple(item for host in HOSTS
                     for item in ('--cap-axis', ids[host] + ':6,12'))
    if not str(stated).strip():
        return ()
    flags = []
    for entry in str(stated).split(';'):
        host, _, rungs = entry.strip().partition(':')
        require(host in ids, 'CAP_HOST_UNKNOWN:' + host)
        require(bool(rungs), 'CAP_HOST_NEEDS_RUNGS:' + host)
        flags += ['--cap-axis', ids[host] + ':' + rungs]
    return tuple(flags)


def _atten_axis_flags(stated):
    """``--atten-axis`` per cell; unset leaves the built-in ladder in place.

    2026-09-17 (오너 결정: "3 단위로 두 셀 다 적용해").  Scoped to the **cell**, not a
    UE, so the value is ``<nci>:<rungs>`` and the baseline (the declared
    ``dl-rf-attenuation`` baseline, ``0.0`` dB) is *not* a rung -- the same
    convention the cap ladder uses for "uncapped".

    Unset means the axis still appears when ``txAttenuationDb`` is an exposed
    kind, on the built-in 0/6/12 ladder.  Stating it here is how the step size
    is chosen without touching the library default.
    """
    if not str(stated or '').strip():
        return ()
    flags = []
    for entry in str(stated).split(';'):
        cell, _, rungs = entry.strip().partition(':')
        require(bool(cell), 'ATT_CELL_MISSING')
        require(bool(rungs), 'ATT_CELL_NEEDS_RUNGS:' + cell)
        flags += ['--atten-axis', cell + ':' + rungs]
    return tuple(flags)


def _pf_axis_flags(ids, stated):
    """``--pf-axis`` for each named host; unset leaves the family out entirely.

    The cap defaults to a ladder for all three UEs when unset.  PF must not:
    ``_axis_specs`` builds a scheduler-weight axis for every UE an intent names
    as soon as ``pfWeight`` is an exposed kind, and the built-in ladder has four
    rungs, so an unset spec multiplies the frozen catalogue by 4^3.  Naming a
    single rung for a UE holds that UE at one weight, which is how the resident
    pair gets opposing weights while the third stays out of the product.
    """
    if not str(stated or '').strip():
        return ()
    flags = []
    for entry in str(stated).split(';'):
        host, _, rungs = entry.strip().partition(':')
        require(host in ids, 'PF_HOST_UNKNOWN:' + host)
        require(bool(rungs), 'PF_HOST_NEEDS_RUNGS:' + host)
        flags += ['--pf-axis', ids[host] + ':' + rungs]
    return tuple(flags)


def preference_profile():
    """The owner preference profile this attempt ranks ``T`` under, or nothing.

    **Absence means the bare default rule** ``lexicographic(D_max, D_mean)`` --
    exactly what every episode recorded so far actually ran under -- so a run
    that asks for nothing is never silently reinterpreted.  The six-case matrix
    of section 7 has to pass ``P1``/``P2``/``P3`` explicitly: a case *label*
    like C4 is not a profile, which is how six labelled cases came to run under
    one unlabelled ranking.

    There is no CLI flag on the child for this.  The sitting reads
    ``AIC_PREFERENCE`` where it builds its authorization
    (agent.py::_preference_for) and inherits it from this process, because the
    subprocess.run that starts the sitting passes no ``env=`` of its own.  That
    reader falls back to the default for any name it does not recognise, so an
    unknown name is refused *here* -- a typo must not rank a declared case
    under the default and record no trace of having done so.
    """
    name = (os.environ.get('AIC_PREFERENCE') or '').strip()
    require(not name or name in PREFERENCE_PROFILES,
            'UNKNOWN_PREFERENCE_PROFILE:' + ','.join(PREFERENCE_PROFILES))
    return name or None


def llm_configuration_preflight(*, model=None, environment=None):
    """Validate the chosen backend's configuration without discovery or network I/O."""
    env = (os.environ if environment is None else environment).get
    method = env('AIC_METHOD') or 'three-agent'
    if model is None and method == 'deterministic':
        return
    if model is None:
        model = env('AIC_ROLE_MODEL') or 'claude-sonnet'
        if method in ('internal-monolith', 'basic-monolith'):
            model = env('AIC_MONOLITH_MODEL') or model
    if model in ('deterministic', 'rule-greedy'):
        return
    if model.startswith('local:'):
        require(bool(model[6:].strip()) and bool(env('LITELLM_BASE_URL')),
                'LLM_LOCAL_ENDPOINT_REQUIRED')
    elif model in ('claude-sonnet', 'claude-opus'):
        require(bool(env('ANTHROPIC_API_KEY') or env('ANTHROPIC_AUTH_TOKEN')),
                'LLM_ANTHROPIC_CREDENTIAL_REQUIRED')
        if model == 'claude-sonnet':
            require(bool(env('AIC_CLAUDE_MODEL_ID')), 'LLM_CLAUDE_MODEL_ID_REQUIRED')
    elif model in ('gpt-4o', 'gpt-4o-mini', 'codex'):
        require(bool(env('OPENAI_API_KEY')), 'LLM_OPENAI_CREDENTIAL_REQUIRED')
    elif model in ('gemini-pro', 'gemini-flash'):
        require(bool(env('GOOGLE_API_KEY')), 'LLM_GOOGLE_CREDENTIAL_REQUIRED')
    else:
        require(model in ('llama-3', 'llama-3-70b', 'phi-3', 'mistral', 'qwen'),
                'LLM_UNKNOWN_BACKEND')


def command_for(root, mapping):
    mapping_by_host(mapping)
    ids = {host: host for host in HOSTS}  # role labels; the sitting resolves each to its current id
    env = os.environ.get
    # Every override below defaults to the value the formal episodes have always
    # used, so an unset environment produces the identical argv the guard test
    # pins.  They exist for the single-probe calibration brief, which needs a
    # no-LLM arm, ~20 s observation phases and one cell -- none of which may
    # silently become the formal run's settings.
    #
    # One setting deliberately does not appear in this argv: AIC_PREFERENCE.
    # There is no CLI flag for the owner preference -- the sitting reads it
    # where it builds its authorization (agent.py::_preference_for) and the
    # child inherits this process's environment, since subprocess.run below
    # passes no env= of its own.  The episode records which profile it actually
    # ranked under in T.preference.rule, so the argv is not the whole contract.
    # preference_profile() validates the name in preflight, and --preference
    # sets it; until both existed the variable was read by the sitting and set
    # by nothing, so every episode ranked under the bare default.
    method = env('AIC_METHOD') or 'three-agent'
    # 'deterministic' is the documented "no LLM for this role" model name, so a
    # deterministic arm keeps the flag shape and spends no proxy credential.
    # Use the public backend label; the backend records the configured provider
    # model separately. AIC_ROLE_MODEL may select a cloud or local:<model> route.
    role_model = ('deterministic' if method == 'deterministic'
                  else (env('AIC_ROLE_MODEL') or 'claude-sonnet'))
    entry = (str(REPO/'main.py') if env('AIC_ENTRY') == 'main'
             else str(DIRECTORY/'authenticated_live_entry.py'))
    # A monolith arm carries the whole method in one model, so it takes
    # --monolith-model and none of the three role flags; giving it role models
    # would name agents it does not have, and giving it no model at all would
    # silently run it modelless. The three-agent arms are unchanged.
    if method in ('internal-monolith', 'basic-monolith'):
        model_flags = ['--monolith-model', env('AIC_MONOLITH_MODEL') or role_model]
    else:
        model_flags = ['--target-agent-model', role_model,
                       '--control-agent-model', role_model,
                       '--trajectory-agent-model', role_model]
    return [sys.executable, '-u', entry,
            '--live', '--profile', str(root/'profile.json'), '--runs-root', str(root/'runs'),
            '--no-gui', '--agent', '--method', method,
            *model_flags, '--intents-json', str(root/'intents.json'),
            # The per-owner (g,d) mode table and the cross-owner deadline quota
            # of integrated-reply section 3.  They come from the pinned pilot,
            # not from root/: they key on reqIds and owner strings, which the
            # per-attempt ueId remap does not touch, so the signed domain is
            # the same bytes every attempt.  Without this the sitting expands
            # the unshaped 108, not the authorized 54 -- intents.json's own
            # domain.expansionCaveat says the expansion cannot enforce the
            # table, so the flag carrying it is not optional.
            '--answers', str(pilot_answers()),
            # 생각 예산은 0 으로 낮춘다 -- 판이 UE 의 gNB 문맥 수명 안에 끝나야 한다.
            # 출력 상한은 역할 기본값(4000)과 같게 둔다.  **이유는 대칭이지 절단이 아니다.**
            # 2026-09-21 정정: 한때 "3000 이면 형성 JSON 이 잘려 복구가 돈다" 고 적었는데
            # 그것은 오진이었다 -- 근거로 쓴 출력 10065·4901 은 **복구까지 합산한 값**이고
            # 첫 응답은 둘 다 온전한 JSON 으로 파싱됐다.  복구를 부른 것은 절단이 아니라
            # **제약 위반**(변경 항목 9개가 상한 4개 초과, cap/PF 동시 선택, 미인가 완화
            # 조합)이었다.  그러니 상한을 올려도 그 죽음은 안 막힌다.  그래도 3000 대신
            # 4000 을 쓰는 이유는 하나뿐이다: `monolith-form` 이 4000 이므로, 여기만
            # 3000 이면 전선에 실제로 오르는 유일한 값에서 방식 간 차이를 내가 만든다.
            #
            # 그리고 **세 방식에 같은 지시를 준다.**  여기서 three-agent 의 세 역할만
            # 덮고 monolith 는 안 덮어서, monolith 만 생각 예산 2000~12000 을 받고
            # three-agent 만 0 을 받았다.  방식 차이가 아니라 내가 만든 불공정이다.
            # (지금 서비스되는 모델 id 로는 생각 예산이 전선에 오르지도 않지만 --
            #  `sentOptions.notSent` 참조 -- 지시가 어긋난 채로 두면 프록시가 바뀌는
            #  날 조용히 차이가 생긴다.)
            '--generation', 'target=4000:0', '--generation', 'control=4000:0',
            # **선택 호출은 전부 2000.**  2026-09-23 결정(시나리오 작성자): 3A 의
            # trajectory 와 IM 의 monolith-select 는 **같은 시스템 프롬프트**
            # (sha256 753884081ea7) 를 쓰면서 한도가 2000 대 1000 이었다 -- 짝지은
            # 선택기 비교가 토큰 예산 2배 차이로 오염돼 있었다.  BM 은 1500 이었다.
            # 2000 은 기존 3A 허용량을 깎지 않는 쪽으로 고른 공통값이고, 최적이라는
            # 주장이 아니다.  형성 호출은 4000 유지.
            '--generation', 'trajectory=2000:0',
            '--generation', 'monolith-form=4000:0',
            '--generation', 'monolith-select=2000:0',
            '--generation', 'basic-monolith=2000:0',
            *prepared_board(root, ids),
            # exp_metrics.md section 5 stores the condition with the episode so results
            # are reported per condition instead of pooled. This association -- ue1 and ue2
            # sharing gnb2 with ue3 alone on gnb1 -- is the scenario's contended family.
            '--condition', 'name=' + (os.environ.get('AIC_CONDITION')
                                      or 'contention-boundary'),
            # The condition is also where the models learn what environment they
            # are reasoning about: ``tools/liveconsole/agent.py::_predictor_state``
            # reads offeredLoadMbps, cellCapacityMbps and prbTotal out of it and
            # falls back to DEFAULT_OFFERED_LOAD_MBPS = 4.0,
            # DEFAULT_CELL_CAPACITY_MBPS = 5.0 and DEFAULT_PRB_TOTAL = 24.0 for
            # anything the condition leaves unstated.  Passing only the name left
            # every episode describing a 24 PRB deployment offering 4 Mbps per UE
            # over 5 Mbps cells, while the senders actually ran at 10 Mbps on
            # 38 PRB cells that measured 10.4 Mbps to a single UE.  With a 9.0 Mbps
            # requirement no candidate can be predicted satisfiable under those
            # numbers, and none was: ``predictedTarget`` is "none" in 46 of 46
            # candidates across the five episodes of 2026-09-14 evening
            # (AGENT-OUTPUT-ANALYSIS-20260914T2140.md section 1).  That uniform
            # answer is an artefact of the input, not a finding about the radio.
            #
            # cellCapacityMbps is the measured deployment point, not an estimate:
            # gnb1's two UEs summed 14.5 Mbps and gnb2's single UE 14.6-16.9
            # (ota-operating-point-20260914).  The conservative 14.5 is used for
            # both cells.  Every value stays overridable so a different bed states
            # its own.
            '--condition', 'offeredLoadMbps=' + (
                os.environ.get('AIC_OFFERED_LOAD_MBPS') or '1'),
            '--condition', 'cellCapacityMbps=' + (
                os.environ.get('AIC_CELL_CAPACITY_MBPS') or '14.5'),
            '--condition', 'prbTotal=' + (os.environ.get('AIC_PRB_TOTAL') or '38'),
            # 2026-09-23: 캠페인은 **판 스스로** 적는다.  `condition.name` 은 코퍼스
            # 이름이라(`pilot38-v4.5-L10-P1`) 부분 v4.6(`blocks18-v46`)과 재시작한
            # v4.6(`blocks18-v46r`)이 같은 이름을 단다 -- 셈 정책도 같아서 지표가 둘을
            # 한 코호트로 합친다.  원장의 `campaign` 은 판 기록 밖에 있어 지표가 못 본다.
            *(('--condition', 'campaign=' + os.environ['AIC_CAMPAIGN'])
              if os.environ.get('AIC_CAMPAIGN') else ()),
            # A trial that ends in INCIDENT_LOCKDOWN can leave an APPLIED_VERIFIED
            # policy behind, and every later sitting is then refused before it
            # submits anything ("withdraw policy <id> on UE <n>").  The framework
            # will archive the record and withdraw it, but only when the operator
            # authorises it explicitly, so the authorisation is an environment
            # switch the campaign sets deliberately -- never a silent default.
            *(['--withdraw-verified-scope']
              if os.environ.get('AIC_WITHDRAW_VERIFIED_SCOPE') == '1' else []),
            '--block', os.environ.get('AIC_BLOCK') or '0',
            '--repetition', os.environ.get('AIC_REPETITION') or '0',
            # B and H come from the pilot exp_metrics.md section 1 asks for. Decomposing the
            # one episode that finished: Target and Control took 121.2 s, prepEnd to t0 was
            # 0.0 s, and the controller needs about 10 s to freeze the header cohort. With
            # the prepared board removing the 121.2 s, a UE has to hold for its own attach
            # (20-25 s) plus that 10 s plus B+H. ue1's fifteen measured lives top out at
            # 70.6 s, so 20/30 asked for 80-85 s and could never be met. 10/15 asks for
            # 55-60 s, which three of those fifteen reach. H >= B holds, H stays a whole
            # number of the frozen 5 s bins, a trial keeps its full settle+window of 6 s,
            # and the same B and H apply to every method.
            '--budget', env('AIC_BUDGET') or '4',
            # 2026-09-16, owner's call: the overall wall-clock limit is removed. Every
            # sitting that died on DEADLINE today died because a UE was gone -- a hardware
            # fault, not a cost of the control being measured -- so the clock must not end
            # the episode; it waits. AIC_DEADLINE_S=off omits the flag, main.py leaves
            # deadline_ms None and agent.py::_deadline_spent skips the check altogether.
            # Set a number to put B back.
            *([] if (env('AIC_DEADLINE_S') or '10').lower() in ('off', 'none', '0')
              else ['--deadline-s', env('AIC_DEADLINE_S') or '10']),
            '--horizon-s', env('AIC_HORIZON_S') or '15',
            # The reply fixes time zero at input release, before any
            # method-specific preparation, so every method is charged the same
            # clock.  That is exactly --timing-mode cold-start; the default
            # 'prepared' starts B after preparation and made the 240/60/480
            # definitions unenforceable.
            '--timing-mode', env('AIC_TIMING_MODE') or 'cold-start',
            '--formation-deadline-s', env('AIC_FORMATION_S') or '240',
            '--decision-deadline-s', env('AIC_DECISION_S') or '60',
            '--boundary', 'exogenous:unique-ip-three-service-trigger', '--initial-measurement',
            # 2026-09-23 결정 §5.1: 그 기준 관측을 **정식 첫 시행**으로 센다.
            # 이전 판들은 elapsed 0 · counted false 로 적어, T0_MET 8판의 달성
            # 시각이 0 인데 실제 창 종료는 t0 에서 23~97초 뒤였다.
            '--formal-reference-trial',
            # 2026-09-23 결정 §3: 유효 결과가 이전 최고 이하면 완전 설정을
            # 유지하고 그것이 다음 시행의 복구 기준선이 된다.  이전 런타임은
            # 목표를 충족한 제어 시행 24건 전부를 C0 로 되돌렸다.
            '--retain-on-improvement',
            '--observe', env('AIC_OBSERVE_GOODPUT') or 'dlGoodputMbps=1000:5000:60000',
            '--observe', env('AIC_OBSERVE_DEADLINE') or 'deadlineSuccessRatio=1000:5000:60000',
            # v4.4 운영자 요구(cellGoodputMbps@cell@<nci>).  **새 계측이 아니다** -- 이미
            # 재고 있는 per-UE goodput 을 servingCell 이 말해 주는 소속으로 묶어 더한 값이라
            # goodput 과 같은 창·통계를 쓴다.  이 줄이 없으면 intake 검사표가
            # `observation.cellGoodputMbps` 를 묻고, 답 없는 질문 하나가 **판 전체를 제출 전에
            # 거절시킨다** (2026-09-23 시도 361).  관측 유효기간은 KPI 최솟값이 지배하므로
            # 여기도 같은 60000 을 준다.
            # 창·유효기간은 dlGoodputMbps 와 **같아야 한다**.  같은 표본을 타므로 창이
            # 다르면 coverage 가 달라지고, 무엇보다 **관측 유효기간은 번들의 KPI 최솟값이
            # 지배한다** -- 여기에 60000 을 주면 다른 둘이 120000 이어도 번들 전체가
            # 60000 으로 줄어 답이 늘 낡는다 (2026-09-14 에 servingCell 10초가 같은 식으로
            # 판 하나를 0 시행으로 만들었다).  run_formal_v3.sh 의 다른 세 줄과 같은 값이다.
            '--observe', env('AIC_OBSERVE_CELL_GOODPUT') or 'cellGoodputMbps=1000:15000:120000',
            # v4.7 사업자 에너지 인텐트(cellTxAttenuationDb@cell@<nci>, 오너 2026-09-25).
            # 인텐트가 없는 판에도 규칙만 있을 뿐 아무것도 재지 않는다; 있는 판에서 이 줄이
            # 없으면 intake 가 `observation.cellTxAttenuationDb` 를 묻고 판 전체가 제출 전에
            # 거절된다(셀 인텐트 여섯 층의 5번).  유효기간은 다른 셋과 같다.
            '--observe', env('AIC_OBSERVE_CELL_ATTENUATION') or 'cellTxAttenuationDb=1000:15000:120000',
            # NOT observed here, and the reason is worth the line.  The
            # continuity statistic rides the goodput samples, so its 30 s
            # built-in window against goodput's 15 s reports coverage 0.57 where
            # dlGoodputMbps reports 1.00 -- cosmetic noise in every trial's thin
            # list.  Passing `--observe` for it looked like the fix and was not:
            # the rule then carries the table's deliberate ``floor=inf`` into the
            # sitting document, and JCS canonicalisation refuses a non-finite
            # number, so the whole episode died before its first trial
            # (2026-09-17, one episode lost).  Aligning the window needs a finite
            # floor, and that is a scenario number (0.5 L1 in the redesign) which
            # is the owner's to name -- not something to invent to quiet a log.
            # servingCell is measured too, and an observation bundle's validity
            # is the MINIMUM over its KPIs (agent.py::_valid_until and
            # intake.py::earliest_valid_until both use min()).  Passing only the
            # two above left servingCell on its built-in default of 10 s, so the
            # bundle expired 10 s after windowEnd no matter what validity the
            # other two were given -- measured 2026-09-14: windowEnd
            # 19:06:47.394, validUntil 19:06:57.394.  A decision call takes 7 s
            # (trajectory) to 122 s (control), so every answer arrived stale,
            # the executor re-observed and asked again, and after two stale
            # answers the deterministic rule decided.  One basic-monolith
            # episode ran ZERO trials that way and was still recorded
            # BUDGET_EXHAUSTED.  Every KPI kind that is measured has to be
            # given a validity here, or the shortest one sets it for all.
            '--observe', env('AIC_OBSERVE_CELL') or 'servingCell=1500:1500:120000',
            '--axes', env('AIC_AXES') or 'servingCell,dlPrbCap',
            # AIC_CAP_HOSTS names which UE hosts carry a cap ladder and with which
            # rungs (``ue3:5`` repeated by comma).  A host left out gets no cap
            # axis at all, which is how the probe keeps a non-intent UE entirely
            # out of the frozen action space.
            *_cap_axis_flags(ids, env('AIC_CAP_HOSTS')),
            # AIC_PF_HOSTS does the same for the scheduler weight, and has to:
            # an unset PF spec gives every UE the four-rung default ladder.
            *_pf_axis_flags(ids, env('AIC_PF_HOSTS')),
            *_atten_axis_flags(env('AIC_ATT_CELLS')),
            # 12 configurations including baseline -- the candidate budget the
            # integrated reply section 4 sets for Control and for the monolith
            # arms alike.  The flag is ELEVEN because ``retain`` counts the
            # candidates *besides* the baseline: ``validate_control_candidates``
            # prepends C0 unconditionally and then caps on ``len(kept) - 1``, so
            # 11 builds the drop's twelve and 12 would build thirteen.  Pinned
            # by counting the board, not by reading this constant, in
            # ``tests/test_atomic_formal_run_guarded.py``.  AIC_RETAIN overrides
            # it for a calibration probe that must not spend a full board.
            # The ceiling is the frozen catalogue's cardinality limit, not a
            # budget: exceeding it refuses the epoch.  512 covered the
            # steering+cap scope.  The revision's three families declare
            # (2*4*2)^3 = 4096 raw, 1,000 after the same-UE cap/PF exclusion and
            # 696 after the four-changed-entry maximum, so a run under that
            # scope would be refused at 512 -- and a ceiling is exactly the kind
            # of silent narrowing the revision forbids.  AIC_MAX_CATALOG states
            # the ceiling the case was frozen under; the admissible count itself
            # is reported from the board, never inferred from this number.
            '--max-catalog', env('AIC_MAX_CATALOG') or '512',
            '--retain', env('AIC_RETAIN') or '11',
            '--quality-thresholds', '0,0.25,0.5,1.0',
            *(() if env('AIC_STOP_AFTER_RELAXED') == '0' else ('--stop-after-relaxed-success',)),
            '--cells', env('AIC_CELLS') or '12345678,87654321']


def check_snapshot(value, spec, session, pinned):
    expected_schema = 'flow-goodput-snapshot/1' if spec['role']=='receiver' else 'tagged-echo-snapshot/1'
    require(value.get('schemaVersion') == expected_schema and value.get('sessionId') == session
            and value.get('flowId') == spec['flowId'] and value.get('status') == 'running',
            'SOURCE_SNAPSHOT_INVALID')
    require(value.get('clockId') == pinned[spec['host']]['bootId'], 'SOURCE_BOOT_CHANGED')
    require(value.get('interface') == {key: val for key, val in pinned[spec['host']].items()
                                      if key != 'bootId'}, 'SOURCE_INTERFACE_CHANGED')
    now, observed = value.get('remoteNowMs'), value.get('observedAtMs')
    require(all(type(v) in (float,int) and math.isfinite(v) for v in (now,observed))
            and 0 <= now-observed <= 1500, 'SOURCE_SNAPSHOT_STALE')
    require(value.get('sourceLog') == f"/tmp/aic-{session}/{spec['slot']}.jsonl", 'SOURCE_LOG_CHANGED')
    if spec['role'] == 'receiver':
        require(value.get('measurementDefinition') == 'tcp-application-payload-consumed'
                and isinstance(value.get('connection'),dict)
                and value['connection'].get('peerIp') == '192.168.70.135'
                and all(type(value.get(key)) is int and value[key] >= 0
                        for key in ('payloadBytes','tunRxBytes')), 'FLOW_COUNTER_OR_CONNECTION_MISSING')
    else:
        counters = value.get('countersByDeadlineMs',{})
        for deadline in ('200','300'):
            row = counters.get(deadline,{})
            require(all(type(row.get(key)) is int and row[key] >= 0
                        for key in ('issued','eligible','completed'))
                    and row['completed'] <= row['eligible'] <= row['issued'], 'ECHO_COUNTERS_MISSING_OR_INVALID')
    return value


def wait_ready(remote, spec, *, listener=False, seconds=8):
    end = time.monotonic()+seconds
    while True:
        receipt = remote.call(spec['endpoint'], 'inspect', spec=spec, listener=listener)
        if receipt.get('ready') is True:
            require(isinstance(receipt.get('sourceOwner'), dict) and isinstance(receipt.get('owner'), dict),
                    'SOURCE_OWNERSHIP_MISSING')
            if listener:
                require(isinstance(receipt.get('listener'), dict), 'LISTENER_OWNERSHIP_MISSING')
            return receipt
        require(time.monotonic() < end, 'SOURCE_OWNER_OR_LISTENER_NOT_READY:'+spec['slot'])
        time.sleep(.1)


def archive_sources(remote, endpoint, root):
    result = remote.call(endpoint, 'archive', timeout=15)
    require(not result.get('errors'), 'SOURCE_ARCHIVE_INCOMPLETE')
    dest = root/'sources'/endpoint
    dest.mkdir(parents=True)
    for item in result['files']:
        name = item['name']
        require(isinstance(name,str) and Path(name).name == name and name not in ('.','..'), 'ARCHIVE_PATH_INVALID')
        data = base64.b64decode(item['base64'], validate=True)
        require(hashlib.sha256(data).hexdigest() == item['sha256'], 'ARCHIVE_HASH_MISMATCH')
        with (dest/name).open('xb') as stream:
            stream.write(data)
    return {'files': [{'name': row['name'], 'sha256': row['sha256']} for row in result['files']]}


def submission_status(root, invoked, cli_exit=None):
    episodes = []
    for path in (root/'evidence').glob('*-episode.json'):
        try:
            record = json.loads(path.read_text())
            if (isinstance(record,dict) and str(record.get('schemaVersion','')).startswith('agent-episode/')
                    and record.get('sessionMode') == 'LIVE' and record.get('episodeId')):
                episodes.append({'path': str(path.relative_to(root)), 'sha256': sha(path),
                                 'episodeId': record['episodeId'], 'termination': record.get('termination')})
        except (ValueError,OSError):
            pass
    if episodes:
        return 'STARTED_EPISODE', episodes
    if not invoked:
        return 'REFUSED_BEFORE_SUBMISSION', []
    output = root/'live-sitting.stdout'
    if cli_exit == 3 and output.is_file():
        lines = output.read_text(errors='replace').splitlines()
        if ('refused before anything was submitted:' in lines
                and not any(line.startswith('confirmation       :') for line in lines)):
            return 'FRAMEWORK_REFUSED_BEFORE_SUBMISSION', []
    return 'SUBMISSION_UNKNOWN_RECONCILIATION_REQUIRED', []



def control_header_dir():
    """Where the Campaign-5 action producer reads the per-UE E2SM-RC control headers.

    Its ``--header-dir``; ``scripts/hardware/env.sh`` names it ``HW_RUNTIME_DIR``.
    A function, not a constant: importing this module reads no environment.
    """
    return Path(os.environ.get('AIC_CONTROL_HEADER_DIR') or (Path.home() / 'rlive'))

_CONTROL_HEADER_GUAMI = (('mcc', 'RC_UE_GUAMI_MCC'), ('mnc', 'RC_UE_GUAMI_MNC'),
                         ('mnc_digit_len', 'RC_UE_GUAMI_MNC_LEN'),
                         ('amf_region_id', 'RC_UE_AMF_REGION_ID'),
                         ('amf_set_id', 'RC_UE_AMF_SET_ID'),
                         ('amf_pointer', 'RC_UE_AMF_POINTER'))


def control_header_rows(deployment, mapping):
    """``{host: header}`` for every pinned UE, from its newest fresh indication.

    Why this exists: the action producer fires a UE-scoped control (PRB cap, PF
    weight) only when a ``<host>-hdr.env`` file names the UE's *current*
    ``amfUeNgapId`` and ``ran_ue_id`` (``live_worker._resolve_identity``).  On
    2026-09-14 the files still named the 11:31 attachments (1847/1848/1841)
    while the UEs had re-attached to 2116/2123/2125, so every cap/PF policy was
    created and BOUND at the A1 layer, nothing reached the RAN, ``RAN.UE.PfWeight``
    stayed 1.0, and the corroborated readback locked the trial down.  Steering
    never showed it because its producer resolves identity from KPM directly.

    The pinned mapping is the Kernel's own identity join, so headers written
    from it cannot disagree with the UEs the attempt addresses.  The newest
    indication wins when an id is fresh on two nodes: after a handover the
    amfUeNgapId is kept but the node and ``ran_ue_id`` change, and the
    destination is the newer record.
    """
    from tools.g3ota.composition import KpmTail, live_topology
    from assurance.live.pin_to_cell_driver import LiveTiming, kpm_node_nb_id
    topology = live_topology(deployment.binding, deployment.capability)
    now_us = datetime.now(timezone.utc).timestamp() * 1_000_000
    bound_ms = LiveTiming().freshness_bound_ms
    wanted = {int(ue): host for ue, host in mapping.items()}
    found = {}
    for line in KpmTail(deployment.kpm_jsonl_path).read_new_lines():
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict) or row.get('event') != 'kpm_indication':
            continue
        node, nb, epoch, at = (row.get(k) for k in ('e2_node', 'nb_id', 'connection_epoch', 'recv_unix_us'))
        if not (type(nb) is int and type(epoch) is int and type(at) is int
                and nb in topology.nb_id_to_nci and kpm_node_nb_id(node) == nb
                and topology.expected_epochs.get(node) == epoch
                and 0 <= (now_us - at) / 1000 <= bound_ms):
            continue
        for ue in row.get('ues') or ():
            amf, ran, guami = ue.get('amf_ue_ngap_id'), ue.get('ran_ue_id'), ue.get('guami')
            if type(amf) is not int or amf not in wanted or type(ran) is not int \
                    or not isinstance(guami, dict) \
                    or any(type(guami.get(key)) is not int for key, _ in _CONTROL_HEADER_GUAMI):
                continue
            host = wanted[amf]
            if host in found and found[host]['receivedUnixUs'] >= at:
                continue
            found[host] = {'RC_HEADER_RRC_UE_ID': ran, 'RC_HEADER_AMF_UE_NGAP_ID': amf,
                           **{name: int(guami[key]) for key, name in _CONTROL_HEADER_GUAMI},
                           'nbId': nb, 'connectionEpoch': epoch, 'receivedUnixUs': at}
    return found


def role_identity_path(root):
    return Path(root) / 'ue-identity.json'


def write_role_identity(rows, path):
    """``{"roles": {host: {amfUeNgapId, nbId, connectionEpoch, writtenAtUnix}}}`` for the sitting.

    The sitting resolves a role label to the id the network gives the UE now from
    this file (agent.py::role_identity_resolver); an entry older than its bound is
    treated as unresolved there, so a stalled refresher cannot pin a dead id.

    A host missing from ``rows`` keeps its previous entry, timestamp included, so
    it *ages out* at that bound instead of vanishing the instant one refresh
    cannot see it.  Rebuilding the document from scratch made a momentary gap
    indistinguishable from a sixty-second absence: attempt 159 of 2026-09-16 was
    refused before submission with "UE ue2 has no current amfUeNgapId" while ue1
    and ue3 sat in the same file 47 seconds old and healthy, because ue2 happened
    to be between registrations when the refresh ran.  The bound is the mechanism
    for deciding that; erasure bypasses it.  A UE that really re-registered
    overwrites its entry the moment it reappears.
    """
    path = Path(path)
    stamp = time.time()
    try:
        kept = dict((json.loads(path.read_text(encoding='utf-8')).get('roles') or {}))
    except (OSError, ValueError):
        kept = {}
    roles = {host: entry for host, entry in kept.items() if host not in rows}
    roles.update({host: {'amfUeNgapId': int(row['RC_HEADER_AMF_UE_NGAP_ID']),
                         'nbId': row['nbId'], 'connectionEpoch': row['connectionEpoch'],
                         'writtenAtUnix': stamp}
                  for host, row in rows.items()})
    document = {'roles': {host: roles[host] for host in sorted(roles)}}
    temporary = path.with_name(f'.{path.name}.{os.getpid()}')
    temporary.write_text(json.dumps(document, sort_keys=True) + '\n')
    os.replace(temporary, path)
    return document


def write_control_headers(rows, directory=None):
    """Atomically rewrite each ``<host>-hdr.env`` whose content changed; return what changed."""
    directory = Path(directory) if directory is not None else control_header_dir()
    directory.mkdir(parents=True, exist_ok=True)
    changed = {}
    for host, row in sorted(rows.items()):
        body = ''.join(f'{key}={row[key]}\n' for key in
                       ('RC_HEADER_RRC_UE_ID', 'RC_HEADER_AMF_UE_NGAP_ID',
                        *[name for _, name in _CONTROL_HEADER_GUAMI]))
        target = directory / f'{host}-hdr.env'
        try:
            if target.read_text() == body:
                continue
        except OSError:
            pass
        temporary = directory / f'.{host}-hdr.env.{os.getpid()}'
        temporary.write_text(body)
        os.replace(temporary, target)
        changed[host] = {'amfUeNgapId': row['RC_HEADER_AMF_UE_NGAP_ID'],
                         'ranUeId': row['RC_HEADER_RRC_UE_ID'], 'nbId': row['nbId'],
                         'connectionEpoch': row['connectionEpoch'], 'writtenAt': utc_now()}
    return changed


class SenderRetargeter:
    """Point a new ext-DN sender at a UE that came back on a new tun address.

    During the sitting the UE-side sink and echo client rebind to the new tun by
    themselves (flow_goodput/tagged_echo ``rebind``); the ext-DN sender only knows
    the address it was started with, so a fresh sender slot is started toward the
    new one for the rest of the source window (docs/design/ue-identity-continuity.md).
    A tun that is down is waited for, never guessed.
    """

    def __init__(self, remote, specs, pinned, launches, attempted, lock, log, expires_monotonic,
                 period_s=5.0):
        import threading
        self._remote, self._pinned = remote, {host: dict(value) for host, value in pinned.items()}
        self._senders = {spec['host']: spec for spec in specs if spec['role'] == 'sender'}
        self._launches, self._attempted, self._lock, self._log = launches, attempted, lock, log
        self._expires, self._period = expires_monotonic, period_s
        self._count = {host: 0 for host in self._senders}
        # Every sender keeps reconnecting to its own address for its whole window
        # (flow_goodput.run_sender), so an address one was ever aimed at is served:
        # a second sender there would only compete for the same listener.
        self._served = {host: {self._pinned[host].get('ip')} for host in self._senders}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='sender-retargeter', daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=30)

    def _run(self):
        while not self._stop.wait(self._period):
            for host, base in sorted(self._senders.items()):
                try:
                    identity = self._remote.call(host, 'identity')
                except Exception:
                    continue  # tun down or unreachable: the sink is waiting too
                if identity.get('ip') in self._served[host]:
                    continue
                remaining = int(self._expires - time.monotonic())
                if remaining < 30:
                    continue
                self._count[host] += 1
                spec = dict(base, slot=f"{base['slot']}-r{self._count[host]}", ip=identity['ip'],
                            durationS=min(900, remaining))
                launch = {'spec': spec, 'requestedAt': utc_now(), 'reason': 'tun-rebind',
                          'previousIp': self._pinned[host].get('ip')}
                with self._lock:
                    self._attempted.add(spec['endpoint'])
                    self._launches.append(launch)
                try:
                    launch['reservation'] = self._remote.call(spec['endpoint'], 'start', spec=spec)
                    # A reservation is only a supervisor; the address counts as served once the
                    # sender itself owns its process, so a child that never ran is retried.
                    launch['ownership'] = wait_ready(self._remote, spec)
                    self._log.append({'at': utc_now(), 'host': host, 'slot': spec['slot'],
                                      'previousIp': self._pinned[host].get('ip'), 'ip': identity['ip']})
                    self._served[host].add(identity['ip'])
                    self._pinned[host] = dict(identity)
                except Exception as exc:  # recorded; the next poll tries again
                    launch['errorType'] = type(exc).__name__
                    self._log.append({'at': utc_now(), 'host': host, 'errorType': type(exc).__name__})


class ControlHeaderRefresher:
    """Keep the headers on the pinned UEs' current node for the attempt's life.

    Polls every ``period_s``; each rewrite is appended to ``log`` so a handover's
    identity change is on the record beside the trial that caused it.  A poll
    that raises is recorded and the loop continues -- a missed refresh leaves the
    last good header in place, which the worker then refuses on its own terms.
    """

    def __init__(self, deployment, mapping, log, directory=None, period_s=2.0,
                 rejoin=None, rejoin_every=5, identity_path=None):
        import threading
        self._deployment, self._mapping, self._log = deployment, dict(mapping), log
        self._directory, self._period = directory, period_s
        # A re-registered UE holds a new id; re-joining host -> id keeps the headers
        # and the role identity file on the UE rather than on its dead id.
        self._rejoin, self._rejoin_every, self._identity_path = rejoin, max(1, int(rejoin_every)), identity_path
        self._polls = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='control-header-refresher', daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self):
        while not self._stop.wait(self._period):
            try:
                self._polls += 1
                if self._rejoin is not None and self._polls % self._rejoin_every == 0:
                    try:
                        joined = self._rejoin()
                    except Exception as exc:  # the previous join stays in force
                        self._log.append({'at': utc_now(), 'rejoinErrorType': type(exc).__name__})
                    else:
                        if joined and dict(joined) != self._mapping:
                            self._log.append({'at': utc_now(), 'rejoined': {'was': self._mapping,
                                                                            'now': dict(joined)}})
                            self._mapping = dict(joined)
                rows = control_header_rows(self._deployment, self._mapping)
                if self._identity_path is not None:
                    # Written even when empty: a UE the fresh stream no longer carries must stop
                    # resolving now, not when its last entry ages out.
                    write_role_identity(rows, self._identity_path)
                changed = write_control_headers(rows, self._directory)
                if changed:
                    self._log.append({'at': utc_now(), 'changed': changed})
            except Exception as exc:  # recorded, never fatal to the attempt
                self._log.append({'at': utc_now(), 'errorType': type(exc).__name__})


def run_attempt(base_profile, *, output_dir=DIRECTORY, base_port=6301):
    session = 'formal38guarded-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex
    root = output_dir/session
    root.mkdir(mode=0o700)
    for directory in ('runs','evidence'):
        (root/directory).mkdir()
    # ``otaCompletionVerified`` is **always False, on purpose.**  This wrapper
    # starts an episode and watches the CLI; it has no way to verify that the
    # OTA run completed, so it never claims so -- a CLI exit of zero means the
    # episode was *started*, not that it finished (the test that pins this is
    # ``test_cli_zero_started_episode_is_not_reported_as_ota_completion``).
    #
    # It reads like a failure and it is not.  2026-09-17: 221 sittings all carry
    # ``false`` and I read that as a broken flag and went looking to delete it.
    # The completion evidence lives in ``evidence/AGENT-*-episode.json`` --
    # ``termination.reason`` -- not here.
    report = {'sessionId': session, 'startedAt': utc_now(), 'cliInvoked': False,
              'cliExit': None, 'cliTimedOut': False, 'otaCompletionVerified': False,
              'cleanup': {}, 'sourceArchive': {}}
    remote = None
    attempted = set()
    header_refresher = None
    retargeter = None
    code = 3
    phase = 'preflight'
    try:
        # Refuse missing model configuration before any traffic; never persist
        # credential values or replace the requested method with a fallback.
        require(not AUTHENTICATION_HOLD.exists(), 'LIVE_MODEL_AUTHENTICATION_ON_HOLD')
        llm_configuration_preflight()
        # An unknown profile name is refused before any traffic, never ranked
        # under the default.  The effective profile is not recorded here on
        # purpose: the sitting already carries it in T.preference.rule, and a
        # second copy in this report could disagree with the board that ran.
        preference_profile()
        pilot_intents({'1':'ue1','2':'ue2','3':'ue3'})  # Hash verification before any remote operation.
        report['dependencies'] = dependency_preflight(base_profile, root)
        remote = Remote(session)
        report['sourceDeployments'] = {host: remote.call(host, 'preflight') for host in (*HOSTS,'extdn')}
        pinned = check_identities({host: remote.call(host, 'identity') for host in HOSTS})
        report['initialTunIdentity'] = pinned
        mapping, base_document = join_hosts(base_profile, root/'profile-joined.json')
        from tools.liveconsole.profile import load_live_deployment
        deployment = load_live_deployment(base_profile)
        initial = kpm_dependencies(deployment, mapping)
        rows = control_header_rows(deployment, mapping)
        require(set(rows) == set(HOSTS), 'CONTROL_HEADER_IDENTITY_UNAVAILABLE:'
                + ','.join(sorted(set(HOSTS) - set(rows))))
        report['controlHeaders'] = {'directory': str(control_header_dir()),
                                    'initial': write_control_headers(rows),
                                    'refreshes': []}
        write_role_identity(rows, role_identity_path(root))
        join_dir = root/'identity-rejoins'
        join_dir.mkdir()
        join_count = [0]

        def rejoin():
            join_count[0] += 1
            return join_hosts(base_profile, join_dir/f'join-{join_count[0]}.json')[0]

        header_refresher = ControlHeaderRefresher(
            deployment, mapping, report['controlHeaders']['refreshes'],
            rejoin=rejoin, identity_path=role_identity_path(root)).start()
        # The sources live as long as the horizon plus the settling the
        # observer needs, never the old fixed 600 s: a short reference pass must
        # not leave listeners behind for the next attempt.
        # ``env`` is a local alias inside the argv builder, not a module name --
        # reading it here raised NameError in preflight and refused the first
        # reference attempt before any source started.
        horizon_s = int(os.environ.get('AIC_HORIZON_S') or '15')
        # With B removed the sitting can run as long as its trial budget needs, so the
        # sources, the sender retargeter and the subprocess timeout all follow one cap
        # instead of the old 900 s; otherwise the episode would simply die somewhere else.
        episode_cap_s = max(horizon_s + 120, int(os.environ.get('AIC_EPISODE_CAP_S') or '3600'))
        specs = specs_for(pinned, base_port, episode_cap_s)
        write_json(root/'intents.json', pilot_intents(mapping))
        write_json(root/'profile.json', make_profile(base_document,base_profile,root,mapping,specs,session))
        report['ueHosts'] = mapping
        report['prelaunchKpm'] = initial
        report['launches'] = []
        phase = 'source-start'
        check_identities({host: remote.call(host,'identity') for host in HOSTS}, pinned)
        # Each independent flow may prepare in parallel, but its listener must
        # belong to this attempt before its sender/client starts. Every host is
        # pinned globally above and again before submission, and locally at each
        # launch. A slow unrelated SSH read must not delay an already-ready flow.
        import threading
        launch_lock = threading.Lock()
        cancelled = threading.Event()
        def start_pair(pair):
            try:
                for spec in pair:
                    require(not cancelled.is_set(), 'SOURCE_START_CANCELLED')
                    require(not AUTHENTICATION_HOLD.exists(), 'LIVE_MODEL_AUTHENTICATION_ON_HOLD')
                    identity = remote.call(spec['host'], 'identity')
                    require(identity == pinned[spec['host']], 'TUN_IDENTITY_CHANGED')
                    launch = {'spec': spec, 'requestedAt': utc_now()}
                    with launch_lock:
                        # Lost replies still owe exact session-owned cleanup.
                        attempted.add(spec['endpoint'])
                        report['launches'].append(launch)
                    launch['reservation'] = remote.call(spec['endpoint'],'start',spec=spec)
                    launch['ownership'] = wait_ready(remote,spec,listener=spec['role'] in ('receiver','echo_server'))
            except Exception:
                cancelled.set()
                raise
        pairs = [specs[index:index+2] for index in range(0, len(specs), 2)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(pairs)) as pool:
            futures = [pool.submit(start_pair, pair) for pair in pairs]
            concurrent.futures.wait(futures)
        raise_root_cause([future.exception() for future in futures])
        phase = 'presubmit'
        # Rejoin, but never substitute new IDs into this attempt's policy or logs.
        final_mapping, _ = join_hosts(base_profile,root/'profile-join-presubmit.json')
        require(final_mapping == mapping, 'AMF_IDENTITY_CHANGED_BEFORE_SUBMIT')
        for path, digest in report['dependencies']['deploymentHashes'].items():
            require(sha(Path(path)) == digest, 'DEPLOYMENT_CHANGED_BEFORE_SUBMIT')
        check_identities({host:remote.call(host,'identity') for host in HOSTS},pinned)
        snapshots, owners, sample_instants = {}, {}, {}
        def sample(spec):
            measured = spec['role'] in ('receiver','echo_client')
            value = remote.call(spec['endpoint'],'sample' if measured else 'inspect',spec=spec)
            # The snapshot's own age is remoteNowMs - observedAtMs, stamped by the
            # remote process as it replies; the time before that (ssh, python start,
            # validating the log) does not age the sample, and counting it twice
            # expired live samples three reads in a row (2026-09-15 attempt 36,
            # ue1 heartbeating every 0.5 s).  So the local clock starts at the reply.
            received = time.monotonic()
            return spec, value, received, measured
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for spec, value, started, measured in pool.map(sample,specs):
                ownership = value['ownership'] if measured else value
                require(ownership.get('ready') is True, 'SOURCE_EXITED_BEFORE_SUBMIT')
                owners[spec['slot']] = ownership
                if measured:
                    snapshots[spec['flowId']] = check_snapshot(value['snapshot'],spec,session,pinned)
                    sample_instants[spec['flowId']] = started
        final_kpm = kpm_dependencies(deployment,mapping)
        require(final_kpm['initialAssociation'] == initial['initialAssociation'], 'ASSOCIATION_CHANGED_BEFORE_SUBMIT')
        identity_fields = ('amfUeNgapId','servingNci','e2Node','connectionEpoch','guAmI')
        require(all(all(final_kpm['observations'][ue].get(key) == initial['observations'][ue].get(key)
                        for key in identity_fields) for ue in mapping), 'KPM_IDENTITY_CHANGED_BEFORE_SUBMIT')
        # The 1500 ms bound is the evidence rule and stays; what was wrong was
        # refusing when the round trip (8 SSH samples + the KPM re-read) pushed
        # a sample past it (2026-09-15 attempt 16, the first in ~40 attempts).
        # An expired sample is read again, never stretched: fresh or refused.
        # A flow id names both ends (receiver and ext-dn sender); only the measured end is sampled.
        specs_by_flow = {spec['flowId']: spec for spec in specs if spec['role'] in ('receiver','echo_client')}
        for round_ in range(3):
            expired = [flow_id for flow_id, value in snapshots.items()
                       if value['remoteNowMs']-value['observedAtMs']
                       + (time.monotonic()-sample_instants[flow_id])*1000 > 1500]
            if not expired:
                break
            require(round_ < 2, 'SOURCE_SAMPLE_EXPIRED_BEFORE_SUBMIT:'+expired[0])
            for flow_id in expired:
                spec, value, started, _ = sample(specs_by_flow[flow_id])
                require(value['ownership'].get('ready') is True, 'SOURCE_EXITED_BEFORE_SUBMIT')
                owners[spec['slot']] = value['ownership']
                snapshots[flow_id] = check_snapshot(value['snapshot'],spec,session,pinned)
                sample_instants[flow_id] = started
        # The manifest used to record the module constants here even when
        # AIC_PILOT pointed the run at a different intent set, so the artefact
        # named a pilot hash the episode had not read.  Record what was resolved.
        pilot_dir, pilot_intents_sha, pilot_manifest_sha = _pilot_pins(PILOT)
        manifest = {'sessionId':session,'startedAt':report['startedAt'],'ueHosts':mapping,
                    'initialAssociation':final_kpm['initialAssociation'],'initialAttribution':final_kpm,
                    'associationCatalog':'servingCell@each UE in both cells; 8 assignments',
                    'tunIdentity':pinned,'sourceSnapshots':snapshots,'sourceOwners':owners,'sources':specs,
                    'pilotDir':str(pilot_dir),
                    'pilotIntentsSha256':pilot_intents_sha,'pilotManifestSha256':pilot_manifest_sha,
                    'profileSha256':sha(root/'profile.json'),'intentsSha256':sha(root/'intents.json'),
                    'offeredLoad':{'dlMbpsPerUe':float(os.environ.get('AIC_OFFERED_LOAD_MBPS') or 1),'echoHz':5,'echoPayloadBytes':256},
                    'codeVersion':_code_version()}
        write_json(root/'manifest.json',manifest)
        edge = datetime.now(timezone.utc)
        require(set(final_kpm['observations']) == set(mapping), 'KPM_ATTRIBUTION_INCOMPLETE')
        require(all(0 <= (edge-datetime.fromisoformat(row['observedAt'].replace('Z','+00:00'))).total_seconds()*1000
                    <= final_kpm['freshnessBoundMs'] for row in final_kpm['observations'].values()),
                'KPM_EXPIRED_BEFORE_SUBMIT')
        require(not AUTHENTICATION_HOLD.exists(), 'LIVE_MODEL_AUTHENTICATION_ON_HOLD')
        phase = 'cli'
        report['tunRebinds'] = []
        retargeter = SenderRetargeter(
            remote, specs, pinned, report['launches'], attempted, launch_lock, report['tunRebinds'],
            expires_monotonic=time.monotonic() + episode_cap_s - 10).start()
        with (root/'live-sitting.stdout').open('x') as output:
            report['cliInvoked'] = True
            # This 900 s is the CLI subprocess timeout -- the wall clock this
            # runner allows the whole sitting process -- and NOT a trial budget.
            # The bounds this block actually passes are at lines 513-515: B = 10 s
            # (--deadline-s), H = 15 s (--horizon-s) and 4 dispatched trials
            # (--budget, which main.py:1208 defines as a trial count, not
            # seconds), plus 240 s formation and 60 s per later decision at lines
            # 522-523.  The drop's 480 s EPISODE budget has no flag in this
            # invocation and nothing enforces it; that deviation is recorded in
            # SELECT10-EVIDENCE-PACKAGE-20260914.md section 5.2 and
            # SELECT10-PREFERENCE-LABEL-INTEGRITY-20260914.md section 4, and the
            # values are the owner's call, not this comment's.
            # 900 s is empirical, not arithmetic: 420 s was too short -- every
            # attempt on 2026-09-14 died at 449-451 s with rc=124 and wrote no
            # evidence file, which is why that window had no usable rows (a
            # measured formation of 182 s is most of that).  900 s stays under
            # the conductor's 1500 s wrapper, so the inner timeout fires first
            # and is recorded rather than the outer one killing the run before
            # it can write the failure down.
            result = subprocess.run(command_for(root,mapping),cwd=REPO,stdin=subprocess.DEVNULL,
                                    stdout=output,stderr=subprocess.STDOUT,timeout=episode_cap_s)
        report['cliExit'] = result.returncode
        code = result.returncode if result.returncode >= 0 else 128-result.returncode
    except subprocess.TimeoutExpired:
        report['failure'] = {'phase':phase,'code':'SUBPROCESS_TIMEOUT'}
        report['cliTimedOut'] = phase == 'cli'
        code = 124
    except KeyboardInterrupt:
        report['failure'] = {'phase':phase,'code':'INTERRUPTED'}
        code = 130
    except Exception as exc:
        # A bare type name loses the one fact that identifies the fault: an
        # ImportError's missing module, an OSError's path. Keep the message
        # for the exception classes whose message is not a value.
        report['failure'] = {'phase':phase,
                             'code':str(exc) if isinstance(exc,Refused) else type(exc).__name__}
        if not isinstance(exc,Refused) and isinstance(exc,(ImportError,OSError,AttributeError,NameError,KeyError)):
            report['failure']['detail'] = str(exc)[:400]
        code = 3 if not report['cliInvoked'] else 1
    finally:
        if header_refresher is not None:
            header_refresher.stop()
        if retargeter is not None:
            retargeter.stop()
        for endpoint in sorted(attempted):
            try:
                receipt = remote.call(endpoint,'cleanup',timeout=25)
                report['cleanup'][endpoint] = receipt
                if receipt.get('complete') is not True:
                    code = code or 70
            except Exception as exc:
                report['cleanup'][endpoint] = {'complete':False,'errorType':type(exc).__name__}
                code = code or 70
            try:
                report['sourceArchive'][endpoint] = archive_sources(remote,endpoint,root)
            except Exception as exc:
                report['sourceArchive'][endpoint] = {'complete':False,'errorType':type(exc).__name__}
                code = code or 74
        try:
            status, episodes = submission_status(root,report['cliInvoked'],report['cliExit'])
        except Exception as exc:
            report['evidenceInspectionError'] = type(exc).__name__
            status, episodes = 'SUBMISSION_UNKNOWN_RECONCILIATION_REQUIRED', []
            code = code or 75
        report.update(submissionStatus=status, episodes=episodes, endedAt=utc_now())
        if report['cliInvoked'] and status != 'STARTED_EPISODE':
            code = code or 75
        report['reconciliationRequired'] = (report['cliTimedOut'] or
            status == 'SUBMISSION_UNKNOWN_RECONCILIATION_REQUIRED' or
            any(row.get('complete') is not True for row in report['cleanup'].values()))
        report['outerExit'] = code
        try:
            write_json(root/'exit.json',report)
        except OSError:
            code = code or 74
        # Hard-coded False for the same reason as the report field above: this
        # wrapper cannot verify an OTA completion and must never appear to.
        print(json.dumps({'root':str(root),'sessionId':session,'outerExit':code,
                          'submissionStatus':status,'otaCompletionVerified':False}),flush=True)
    return code


def _direct_children():
    """이 프로세스의 직계 자식 pid (살아 있는 것만)."""
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry/'stat').read_text().rsplit(') ', 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[0] != 'Z' and fields[1] == str(os.getpid()):
            found.append(int(entry.name))
    return found


def _sigterm_as_interrupt(grace_s=10.0):
    """SIGTERM 을 KeyboardInterrupt 와 같은 정리 경로로 (2026-09-23 감사).

    systemd stop/restart 가 SIGTERM 을 보내면 이 러너는 기본 동작으로 즉사해 finally --
    exit.json·원격 부하원 cleanup·원본 보관 -- 가 돌지 않았다(판 20260923T120725 에
    exit.json 없음).  이제 자식(판 CLI 등)에게 SIGTERM 을 넘기고 최대 grace_s 초 정리할
    틈을 준 뒤 KeyboardInterrupt 를 올린다 -- subprocess.run 은 그 예외에 자식을 SIGKILL
    하므로 먼저 기다린다.  두 번째 SIGTERM 은 정리를 끊지 않도록 무시한다.  killpg 는
    쓰지 않는다: 같은 프로세스 그룹에 run_forever.sh 까지 있다.
    """
    import signal
    import threading
    if threading.current_thread() is not threading.main_thread():
        return

    def on_term(_signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        children = _direct_children()
        for pid in children:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        end = time.monotonic() + grace_s
        while children and time.monotonic() < end:
            alive = set(_direct_children())
            children = [pid for pid in children if pid in alive]
            if children:
                time.sleep(0.2)
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # Kept in this file so R1 discovery has a bounded subprocess and imports are
    # inert. Tests replace subprocess.run; they never execute this live branch.
    if len(argv)==3 and argv[0]=='--dependency-check':
        sys.path.insert(0,str(REPO))
        try:
            result = r1_dependencies(Path(argv[1]),Path(argv[2]))
            print(json.dumps({'ok':True,'result':result}),flush=True)
            return 0
        except Exception as exc:
            print(json.dumps({'ok':False,'errorType':str(exc) if isinstance(exc,Refused)
                              else type(exc).__name__}),flush=True)
            return 3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',type=Path,default=REPO/'deployment/liveconsole-profile-3ue-sonnet-run.json')
    parser.add_argument('--output-dir',type=Path,default=DIRECTORY)
    parser.add_argument('--base-port',type=int,default=6301)
    # Unset keeps the default rule lexicographic(D_max, D_mean) -- today's
    # behaviour exactly.  argparse refuses any other name and names the three
    # it accepts, so a mistyped case never falls back to that default.
    parser.add_argument('--preference',choices=PREFERENCE_PROFILES,default=None,
                        help='owner preference profile (integrated reply section 3); '
                             'unset keeps the default rule lexicographic(D_max, D_mean)')
    args = parser.parse_args(argv)
    if args.preference:
        # The child reads this, not an argv flag; it inherits this environment.
        os.environ['AIC_PREFERENCE'] = args.preference
    sys.path.insert(0,str(REPO))
    _sigterm_as_interrupt()
    return run_attempt(args.profile.resolve(),output_dir=args.output_dir.resolve(),base_port=args.base_port)


if __name__=='__main__':
    raise SystemExit(main())
