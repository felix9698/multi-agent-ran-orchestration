"""경계 이후 첫 판에서 오늘 고친 것들을 한 번에 확인한다 (2026-09-18).

폴링하지 않는다 -- 부를 때마다 지금 상태를 찍고 끝낸다.
"""
import glob, calendar, time, os, sys, json
sys.path.insert(0, 'ops')
import arm_compare as AC, control_effects as ce

def _ladders(run_dir):
    """조립된 감쇠 사다리 -- 기준값이 라이브인지 선언 상수인지 한 줄로 드러난다."""
    import re
    try:
        text = open(run_dir + 'live-sitting.stdout', errors='replace').read()
    except OSError:
        return []
    out = []
    for m in re.finditer(r'function cell-tx-attenuation@(\d+).*?txAttenuationDb=\[([^\]]*)\]', text):
        cell, values = m.group(1), m.group(2)
        first = values.split(',')[0].strip().strip("'")
        out.append("감쇠@%s 기준 %s · 사다리 %s" % (cell, first, values[:52]))
    return out


CHECKS = []
for d in sorted(glob.glob("formal38guarded-2026*T*/")):
    name = os.path.basename(d.rstrip('/'))[16:31]
    try:
        started = calendar.timegm(time.strptime(name, '%Y%m%dT%H%M%S'))
    except ValueError:
        continue
    if started < AC.ACTION_SPACE_EPOCH:
        continue
    found = glob.glob(d + 'evidence/AGENT-*-episode.json')
    if not found:
        # 2026-09-18: 완주를 기다리지 않는다.  조립 로그가 이미 답을 갖고 있다 --
        # 이 판(01:37:34)의 `[txAttenuationDb=['0.0', '13.0', ...]` 한 줄이
        # "라이브 기준값을 못 읽었다" 를 완주 전에 말해 줬고, 내 회귀 검사 셋은
        # 그것을 못 잡았다(줄을 직접 줘서 소비자가 비는 상황을 재현 못 함).
        # 2026-09-18: 증거가 없다고 다 "진행중" 이 아니다 -- 거절된 판은 `exit.json` 만 남고
        # 영영 증거를 못 만든다.  그것을 진행중으로 세면 죽은 판이 살아 있어 보인다.
        state = "진행중"
        exit_path = d + 'exit.json'
        if os.path.exists(exit_path):
            try:
                status = str(json.load(open(exit_path)).get('submissionStatus') or '')
            except (ValueError, OSError):
                status = '?'
            if status and status != 'SUBMITTED':
                state = "거절 " + status
        print("  %s %s" % (name, state))
        for line in _ladders(d):
            print("    %s" % line)
        continue
    doc = json.load(open(found[0]))
    trials = doc.get('trials') or []
    supp = (doc.get('execution') or {}).get('supplementary') or []
    atten = [s for s in supp if 'txAtten' in str(s.get('axis'))]

    def _detail(t):
        return str((t.get('kernel') or {}).get('detail') or '')

    mismatch = sum(1 for t in trials if 'REJECTED_CONFIG_MISMATCH' in _detail(t))
    unknown = sum(1 for t in trials if 'UNKNOWN the configuration' in _detail(t))
    settled = sum(1 for t in trials
                  if any((t.get('success') or {}).values()) and not t.get('rolledBack'))
    keys = [s.get('adapterKey') for s in atten]
    base = (trials or [{}])[0].get('configuration') or {}
    baseline = {k.split('@')[-1]: v for k, v in base.items() if 'txAtten' in k}
    outcomes = {}
    for s in atten:
        for e in (s.get('readbackLog') or []):
            outcomes[str(e.get('outcome'))] = outcomes.get(str(e.get('outcome')), 0) + 1

    print("판 %s · %s · %s · %s" % (
        name, doc.get('method'), (doc.get('termination') or {}).get('reason'),
        '깨끗' if not ce.dirt_reasons(doc) else '오염'))
    # 오늘 고친 넷이 각각 라이브에서 무엇으로 보이는지
    print("  [기준값→라이브]  설정불일치 거절 %d  %s" % (
        mismatch, '← 사라졌다' if not mismatch else '← 아직 난다'))
    print("  [조립 UE 가정]   감쇠 어댑터키 %s  %s" % (
        keys or '없음',
        '← 셀로 고쳐졌다' if keys and all('@' in k and k.split('@')[1].isdigit() for k in keys)
        else '← 확인 필요' if keys else ''))
    print("  [이름 체계]      되읽기 %s" % (outcomes or '없음'))
    print("  [dict-bool]      정착 %d (시행 %d) · UNKNOWN %d" % (settled, len(trials), unknown))
    print("  기준선 감쇠 %s" % (baseline or '없음'))
    CHECKS.append(name)

if not CHECKS:
    print("경계(%s) 이후 완주 판 아직 없음 · 지금 %s" % (
        time.strftime('%H:%M:%S', time.localtime(AC.ACTION_SPACE_EPOCH)),
        time.strftime('%H:%M:%S')))
