#!/usr/bin/env python3
"""Launch all live sources and immediately submit one four-intent LIVE sitting."""
import concurrent.futures
import datetime
import hashlib
import json
import shlex
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path('/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN')
SESSION = 'formal38authlight-' + datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
ROOT = REPO/'experiment_results/ota-20260911'/SESSION
ROOT.mkdir()
SOURCE = '/tmp/aic-flow-9140cea9b49e-beaa921ad26a'
HOSTS = [('ue1', None, 6201, 'ue1-data'),
         ('ue2', None, 6202, 'ue2-map'),
         ('ue3', None, 6203, 'ue3-incumbent')]

def run(argv, **kwargs):
    return subprocess.run(argv, text=True, capture_output=True, **kwargs)

def ssh(host, command, *, input=None, timeout=8):
    return run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=3',host,command],
               input=input, timeout=timeout)

def current_tun_ip(host):
    result = ssh(host, "ip -4 -o addr show up dev oaitun_ue1 2>/dev/null", timeout=5)
    words = result.stdout.split()
    if result.returncode or 'inet' not in words:
        raise SystemExit(f'{host}: no current UP TUN')
    value = words[words.index('inet') + 1].split('/', 1)[0]
    if not value.startswith('12.1.1.'):
        raise SystemExit(f'{host}: unexpected TUN address')
    return value

HOSTS = [(host, current_tun_ip(host), port, flow)
         for host, _ip, port, flow in HOSTS]

def start_receiver(row):
    host, ip, port, flow = row
    log = f'/tmp/{SESSION}-{flow}.jsonl'
    out = f'/tmp/{SESSION}-{flow}-receiver.out'
    command = ['timeout','620','python3',SOURCE+'/flow_goodput.py','receiver',
               '--session-id',SESSION,'--flow-id',flow,'--port',str(port),
               '--duration-s','600','--bind-ip',ip,
               '--allow-source-ip','192.168.70.135','--max-rate-mbps','8','--log',log]
    result = ssh(host, 'test ! -e '+shlex.quote(log)+'; setsid -f '+shlex.join(command)
                 +' >'+shlex.quote(out)+' 2>&1')
    return dict(host=host, ip=ip, port=port, flowId=flow, logPath=log, out=out,
                receiverStartExit=result.returncode, receiverStartError=result.stderr)

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    flows = list(pool.map(start_receiver, HOSTS))
for flow in flows:
    deadline = time.monotonic()+5
    while time.monotonic()<deadline:
        state = ssh(flow['host'], f"ss -ltnH '( sport = :{flow['port']} )'", timeout=4)
        if state.stdout.strip():
            flow['listenerReady'] = True
            break
        time.sleep(.05)
    else:
        flow['listenerReady'] = False
    if not flow['listenerReady']:
        raise SystemExit('receiver did not listen: '+json.dumps(flow))
    sender = run(['docker','exec','-d','oai-ext-dn','timeout','610','python3',
                  SOURCE+'/flow_goodput.py','sender','--session-id',SESSION,
                  '--flow-id',flow['flowId'],'--port',str(flow['port']),
                  '--duration-s','590','--receiver-ip',flow['ip'],'--rate-mbps','0.2'], timeout=5)
    flow['senderStartExit'] = sender.returncode
    if sender.returncode:
        raise SystemExit('sender did not start: '+sender.stderr)

echo = dict(host='ue1', port=6204, flowId='ue1-command',
            logPath=f'/tmp/{SESSION}-ue1-command.jsonl')
echo_server = run(['docker','exec','-d','oai-ext-dn','timeout','610','python3',
                   SOURCE+'/tagged_echo.py','server','--session-id',SESSION,
                   '--flow-id',echo['flowId'],'--port',str(echo['port']),
                   '--duration-s','600','--rate-hz','2','--bind-ip','192.168.70.135',
                   '--allow-subnet','192.168.70.134/32'], timeout=5)
if echo_server.returncode:
    raise SystemExit('echo server failed: '+echo_server.stderr)
echo_command = ['timeout','610','python3',SOURCE+'/tagged_echo.py','client',
                '--session-id',SESSION,'--flow-id',echo['flowId'],'--port',str(echo['port']),
                '--duration-s','590','--rate-hz','2','--server-ip','192.168.70.135',
                '--interface','oaitun_ue1','--payload-bytes','256','--reply-drain-s','2',
                '--log',echo['logPath']]
echo_client = ssh('ue1', 'test ! -e '+shlex.quote(echo['logPath'])+'; setsid -f '
                  +shlex.join(echo_command)+' >'+shlex.quote(f'/tmp/{SESSION}-echo.out')+' 2>&1')
if echo_client.returncode:
    raise SystemExit('echo client failed: '+echo_client.stderr)

# Require one fresh real heartbeat from every source before constructing policy ports.
def snapshot(flow):
    cmd = shlex.join(['python3',SOURCE+'/flow_goodput.py','snapshot','--log',flow['logPath'],
                      '--session-id',SESSION,'--flow-id',flow['flowId'],'--max-age-ms','1500'])
    return ssh(flow['host'], cmd, timeout=5)

deadline = time.monotonic()+8
snapshots = {}
while time.monotonic()<deadline:
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(snapshot, flows))
    if all(item.returncode == 0 for item in results):
        snapshots = {flow['flowId']: json.loads(item.stdout) for flow,item in zip(flows,results)}
        break
    time.sleep(.1)
if not snapshots:
    raise SystemExit('all flow sources did not become fresh')
echo_snapshot = ssh('ue1', shlex.join(['python3',SOURCE+'/tagged_echo.py','snapshot',
                    '--log',echo['logPath'],'--session-id',SESSION,'--flow-id',echo['flowId'],
                    '--deadlines-ms','200,300','--max-age-ms','1500']), timeout=5)
if echo_snapshot.returncode:
    raise SystemExit('echo source not fresh: '+echo_snapshot.stderr)
snapshots['ue1-command'] = json.loads(echo_snapshot.stdout)

# Existing identity join emits only AMF-id -> host; it stores no subscriber identifier.
base_profile = REPO/'deployment/liveconsole-profile-3ue-sonnet-run.json'
profile_base = ROOT/'profile-base.json'
identity = run(['python3',
 '/tmp/claude-1000/-home-ran-node1-agentic-ran-coordinator-based-on-ORAN/04937d58-04ff-4a1f-995c-5a0b8c910d31/scratchpad/ue_host_map.py',
 str(base_profile), str(profile_base), 'ue1','ue2','ue3'], timeout=20)
if identity.returncode:
    raise SystemExit('identity mapping failed: '+identity.stderr)
mapping = json.loads(identity.stdout)
by_host = {host: ue for ue,host in mapping.items()}
required = {'ue1','ue2','ue3'}
if set(by_host) != required:
    raise SystemExit('identity mapping incomplete')
profile = json.loads(profile_base.read_text())
profile['description'] = 'Atomic four-intent LIVE sitting with three unique-IP simultaneous flows.'
profile['runsRoot'] = str(ROOT/'runs')
profile['liveConsole']['evidenceDir'] = str(ROOT/'evidence')
profile['liveConsole']['ueHosts'] = mapping
flow_by_host = {row['host']: row for row in flows}
profile['liveConsole']['flowGoodput'] = {
    by_host[h]: {'sourcePath':SOURCE+'/flow_goodput.py','logPath':flow_by_host[h]['logPath'],
                 'sessionId':SESSION,'flowId':flow_by_host[h]['flowId'],'maxAgeMs':1500}
    for h in required}
profile['liveConsole']['taggedEcho'] = {
    by_host['ue1']: {'sourcePath':SOURCE+'/tagged_echo.py','logPath':echo['logPath'],
                    'sessionId':SESSION,'flowId':echo['flowId'],'maxAgeMs':1500}}
profile['intentDefaults'] = []
profile_path = ROOT/'profile.json'
profile_path.write_text(json.dumps(profile,indent=2)+'\n')
intents = {'intents': [
 {'intentId':'I1','owner':'ue1-video','ueId':by_host['ue1'],'priority':1,'weight':3.0,
  'requirement':{'reqId':'I1.r1','kpi':'dlGoodputMbps','op':'>=','value':0.15,
                 'unit':'Mbps','steps':2,'bound':0.1}},
 {'intentId':'I2','owner':'ue2-map','ueId':by_host['ue2'],'priority':2,'weight':2.0,
  'requirement':{'reqId':'I2.r1','kpi':'dlGoodputMbps','op':'>=','value':0.08,
                 'unit':'Mbps','steps':2,'bound':0.04}},
 {'intentId':'I3','owner':'ue3-incumbent','ueId':by_host['ue3'],'priority':3,'weight':1.0,
  'requirement':{'reqId':'I3.r1','kpi':'dlGoodputMbps','op':'>=','value':0.08,
                 'unit':'Mbps','steps':2,'bound':0.04}},
 {'intentId':'I4','owner':'ue1-command','ueId':by_host['ue1'],'priority':1,'weight':3.0,
  'requirement':{'reqId':'I4.r1','kpi':'deadlineSuccessRatio','op':'>=','value':0.5,
                 'unit':'ratio','steps':1,'bound':0.4,'deadlineMs':200,
                 'deadlineSteps':1,'deadlineBound':300}}]}
intents_path = ROOT/'intents.json'
intents_path.write_text(json.dumps(intents,indent=2)+'\n')
(ROOT/'runs').mkdir(); (ROOT/'evidence').mkdir()
shutil.copy2(REPO/'deployment/assurance-live-binding.1.0.0.json',
             ROOT/'assurance-live-binding.1.0.0.json')
manifest = {'sessionId':SESSION,'startedAt':datetime.datetime.now().astimezone().isoformat(),
            'ueHosts':mapping,'initialAssociation':'read from fresh KPM at sitting trigger',
            'associationCatalog':'servingCell@each UE in both cells; 8 assignments',
            'sourceSnapshots':snapshots,'flows':flows,'echo':echo,
            'profileSha256':hashlib.sha256(profile_path.read_bytes()).hexdigest(),
            'intentsSha256':hashlib.sha256(intents_path.read_bytes()).hexdigest()}
(ROOT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')

ids = {key: by_host[key] for key in required}
command = ['python3',str(REPO/'experiment_results/ota-20260911/authenticated_live_entry.py'),'--live','--profile',str(profile_path),'--runs-root',str(ROOT/'runs'),
 '--no-gui','--agent','--method','three-agent','--target-agent-model','claude-sonnet',
 '--control-agent-model','claude-sonnet','--trajectory-agent-model','claude-sonnet',
 '--intents-json',str(intents_path),'--budget','2','--deadline-s','90','--horizon-s','90',
 '--boundary','exogenous:unique-ip-three-service-trigger','--initial-measurement',
 '--observe','dlGoodputMbps=1000:5000:60000',
 '--observe','deadlineSuccessRatio=1000:5000:60000','--axes','servingCell,dlPrbCap',
 '--cap-axis',ids['ue1']+':6,12','--cap-axis',ids['ue2']+':6,12',
 '--cap-axis',ids['ue3']+':6,12','--max-catalog','512','--retain','8',
 '--quality-thresholds','0,0.25,0.5,1.0','--stop-after-relaxed-success',
 '--cells','12345678,87654321']
with (ROOT/'live-sitting.stdout').open('x') as output:
            # 420 s cannot hold a 480 s episode: B and H are both 480 s and
            # formation reaches 182 s, so the sitting needs ~720 s.  Every
            # attempt on 2026-09-14 died at 449-451 s with rc=124 and wrote no
            # evidence.  Matches the guarded runner's 900 s.
    completed = subprocess.run(command,cwd=REPO,stdout=output,stderr=subprocess.STDOUT,timeout=900)
(ROOT/'exit.json').write_text(json.dumps({'exit':completed.returncode,
     'endedAt':datetime.datetime.now().astimezone().isoformat()},indent=2)+'\n')
print(json.dumps({'root':str(ROOT),'sessionId':SESSION,'ueHosts':mapping,
                  'liveSittingExit':completed.returncode}),flush=True)
