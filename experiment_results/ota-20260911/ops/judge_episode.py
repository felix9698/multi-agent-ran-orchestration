#!/usr/bin/env python3
"""한 에피소드를 목표 도달·유지·각 trial 실패 축으로 판정한다.

thin window 는 제어 실패가 아니라 관측 실패이므로 따로 센다: SAFETY_STOPPED 를
'제어가 나빴다' 로 읽으면 조정 성능을 과소평가한다 (2026-09-17 조인 목표 판에서
SAFETY_STOPPED 2건이 모두 deadlineSuccessRatio@ue2 의 빈 coverage 였다).
"""
import json, sys, glob, os

# 오염 판정의 **규칙은 여기 없다.**  `control_effects.dirt_reasons()` 가 유일한
# 구현이고, 이 판정기는 그 사유를 받아 찍기만 한다.
#
# 2026-09-17 에 같은 규칙이 두 파일에 각각 있었다.  그날 구멍을 네 번 고쳤는데
# (총합만 보던 것 · 다른 UE 캡에 가려진 사망 · 한 셀 배치의 정상 총합 · 캡 밑의
# 0.00) 매번 **두 곳을 다 고쳐야** 했다.  한 번만 빠뜨려도 판정기와 집계기가
# 서로 다른 판을 "깨끗하다" 고 부르게 된다.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from control_effects import dirt_reasons, NOCAP_TOTAL_FLOOR

def main(session_glob):
    # An episode.json handed in directly is a session named one level too deep.
    # Without this the glob below comes back empty and the reader is told the
    # sitting died before its first trial -- a bad argument wearing the costume
    # of a broken run (2026-09-17: a four-trial episode was reported as dead).
    if os.path.isfile(session_glob):
        session_glob = os.path.dirname(os.path.dirname(os.path.abspath(session_glob)))
    found = sorted(glob.glob(session_glob + '/evidence/AGENT-*-episode.json'))
    if not found:
        # A sitting that died before its first trial writes no episode document.
        # Raising here lost the whole verdict record for that run (2026-09-17),
        # which is exactly when a reader most needs to be told what happened.
        print('세션 :', session_glob.rstrip('/').split('/')[-1])
        print('에피소드 증거 없음 — 착석이 첫 시행 전에 죽었다.')
        for name in ('exit.json',):
            try:
                doc = json.load(open(os.path.join(session_glob, name)))
            except Exception:
                continue
            print('  outerExit=%s  submissionStatus=%s  failure=%s'
                  % (doc.get('outerExit'), doc.get('submissionStatus'),
                     json.dumps(doc.get('failure'), ensure_ascii=False)))
        tail = os.path.join(session_glob, 'live-sitting.stdout')
        if os.path.exists(tail):
            lines = [ln.rstrip() for ln in open(tail, errors='replace')][-6:]
            for ln in lines:
                print('  |', ln[:200])
        return
    path = found[-1]
    d = json.load(open(path))
    print('세션 :', path.split('/')[-3])
    # 2026-09-17 부터 팔을 섞어 돌린다(three-agent / basic-monolith / internal-monolith).
    # 팔을 안 찍으면 판정문만 보고 어느 팔인지 알 수 없고, 나중에 판정문을 모아 읽을 때
    # 팔 효과와 조건 효과를 가를 수 없다.  `AIC_CONDITION` 에는 팔이 안 들어간다.
    print('팔   : %s  (models=%s)' % (
        d.get('method'),
        ','.join('%s=%s' % (k, v) for k, v in sorted((d.get('models') or {}).items()) if v)))
    print('조건 :', json.dumps(d.get('condition'), ensure_ascii=False))
    t = d.get('termination') or {}
    print('종료 : %s / kernel=%s' % (t.get('reason'), t.get('kernelTermination')))
    r = d.get('retained') or {}
    print('유지 : %s x %s qualified=%s  withdrawalOnlyAxes=%s' % (
        r.get('controlId'), r.get('targetId'), r.get('qualified'), r.get('withdrawalOnlyAxes')))
    # 2026-09-17: 유지가 왜 실패했는지는 이 detail 에만 있다. 여기 없어서
    # 'qualified=False = 유지가 막혔다' 로 오독했는데 실제로는 유지 판이
    # 정상 조립돼 돌다가 409 로 죽은 것이었다.
    if r.get('detail'):
        print('       사유: %s' % str(r['detail'])[:200])
    rt = (r.get('trial') or {}).get('kernel') or {}
    if rt.get('detail'):
        print('       유지시행: %s / %s :: %s' % (rt.get('outcome'), rt.get('stopReason'),
                                                 str(rt['detail'])[:220]))
    print()
    counts = {}
    for tr in d.get('trials', []):
        k = tr.get('kernel') or {}
        outcome, stop = k.get('outcome'), k.get('stopReason')
        win = tr.get('window') or {}
        thin = [a for a, c in (win.get('coverage') or {}).items() if c is not None and c < 0.8]
        unknown = win.get('unknownKpis') or []
        kpis = tr.get('kpis') or {}
        good = {a.split('@')[1]: v for a, v in kpis.items()
                if a.startswith('dlGoodputMbps') and isinstance(v, (int, float))}
        dl = {a.split('@')[1]: v.get('byDeadlineMs', {}).get('2000')
              for a, v in kpis.items() if a.startswith('deadlineSuccessRatio') and isinstance(v, dict)}
        label = stop or outcome or 'NONE'
        # 관측 실패와 제어 실패를 가른다
        kind = '관측' if (stop == 'TELEMETRY_STALE' or (not kpis and label != 'SUCCESS')) else '제어'
        counts[label] = counts.get(label, 0) + 1
        print('trial %-2s %-10s %-22s [%s] 굿풋=%s 마감=%s' % (
            tr.get('trialIndex'), tr.get('controlId'), label, kind,
            {k2: round(v, 3) for k2, v in good.items()} or '-', dl or '-'))
        # `lowDeliveryRunBins` 는 **구조적으로** 0.6 이다 (2026-09-17 확인:
        # 121시행 전부, 두 UE 전부). `_expected` 가 그 KPI 자기 표본의 중앙
        # 간격으로 창을 나누는데, lowRun 표본은 1초 간격인데 30초 창에 18개만
        # 들어온다 -> 18/30 = 0.6 < 0.8. 무작위 손실이 아니므로 매 시행 두 줄씩
        # 소음을 내지 않는다. **어떤 인텐트도 이 축을 쓰지 않으므로 판정 손실은
        # 0** (이 판의 요구조건은 굿풋@ue1>=5.6 · 마감@ue2>=0.9 · 굿풋@ue3>=6.4).
        # 배달 연속성 인텐트를 실제로 쓰게 되면 그때 고쳐야 한다 -- 지금 고치면
        # 캠페인 도중에 증거 모양만 바꾼다.
        STRUCTURAL = 'lowDeliveryRunBins'
        thin = [a for a in thin if not a.startswith(STRUCTURAL)]
        unknown = [a for a in unknown if not a.startswith(STRUCTURAL)]
        if thin:    print('          얇은 창:', thin)
        if unknown: print('          미관측 :', unknown)
    # 오염 판별: 캡이 없는데 5.0 Mbps 미만인 UE 가 있으면 그 판은 UE 하향 장애로
    # 오염된 것이다. 2026-09-17 에 23판 중 7판이 여기 걸렸고, 기준선만 보면
    # 판 2318 처럼 시행 1부터 죽은 판을 놓친다 — 그래서 모든 시행을 본다.
    # 캡에 의한 저하는 정상적인 제어 효과이므로 제외한다.
    def _cells(cfg):
        return tuple(cfg.get('servingCell@%s' % ue) for ue in ('ue1', 'ue2', 'ue3'))
    trials = d.get('trials', [])
    baseline_cells = _cells({k: str(v) for k, v
                             in ((trials[0].get('configuration') or {}) if trials else {}).items()})
    dirty = dirt_reasons(d)
    if dirty:
        print('*** 오염: %s' % ' '.join(dirty[:5]))
        print('***       이 판은 UE 하향 장애로 오염됐다. 집계에서 뺄 것.')
    else:
        print('오염 없음 (무캡 시행의 총합이 정상 대역)')
    print()
    # 실패 detail 을 그대로 싣는다: 2026-09-17 판에서 stopReason 만 보고
    # 'SAFETY_STOPPED = 관측 실패' 로 오독했는데 증거의 실제 사유는 PARTIAL_APPLY 였다.
    for tr in d.get('trials', []):
        k = tr.get('kernel') or {}
        detail = k.get('detail')
        if detail and k.get('stopReason') not in (None, 'SEMANTIC_NON_SUCCESS'):
            print('trial %-2s detail: %s' % (tr.get('trialIndex'), str(detail)[:300]))
    # 증거를 잃은 trial 의 비율.  2026-09-17 에 손으로 세어 보니 네 판 합쳐 14건 중 6건(43%)이
    # `deadlineSuccessRatio@ue2` 코호트를 통째로 잃고 있었다 -- 예산 낭비의 최대 원인인데 매
    # 판정마다 손으로 세지 않으면 보이지 않는다.  그래서 판정이 직접 말한다.
    lost = {}
    counted = 0
    for tr in d.get('trials', []):
        if not tr.get('counted'):
            continue
        counted += 1
        cov = (tr.get('window') or {}).get('coverage') or {}
        for axis in ('deadlineSuccessRatio', 'dlGoodputMbps'):
            if not any(a.startswith(axis) for a in cov):
                lost[axis] = lost.get(axis, 0) + 1
    if counted:
        for axis in sorted(lost):
            print('증거 손실: %-22s %d/%d trial (%.0f%%)'
                  % (axis, lost[axis], counted, 100 * lost[axis] / counted))
    # 조종 쓰기의 **종류**.  2026-09-18: 프로듀서의 503 은 UE 를 기준선 셀로
    # **되돌리는** 쓰기에서만 난다 (복귀 10건 중 4건 / 이탈 204건 중 0건 /
    # 무쓰기 237건 중 0건, 213판에서 Fisher p=1.2e-07).  처음에는 "판의 뒷자리"
    # 로 보였으나 **복귀는 이탈 뒤에만 가능하니 늘 뒷자리**여서 생긴 그림자였다.
    # 복귀는 드물어서(213판에 10건) 매 판 세어 두지 않으면 다음 사례를 놓친다.
    BASE = {'servingCell@ue1': '87654321', 'servingCell@ue2': '12345678',
            'servingCell@ue3': '12345678'}
    items = [(tr.get('trialIndex'), tr) for tr in d.get('trials', [])]
    if (d.get('retained') or {}).get('trial'):
        items.append((len(d.get('trials', [])), d['retained']['trial']))
    moved = False
    writes = []
    said_cause = False
    for idx, tr in items:
        cfg = {k: str(v) for k, v in (tr.get('configuration') or {}).items()
               if k.startswith('servingCell')}
        if not cfg:
            continue
        det = str((tr.get('kernel') or {}).get('detail') or '')
        bad = '503' in det
        kind = '이탈' if cfg != BASE else ('복귀' if moved else None)
        if kind:
            writes.append('자리%s %s %s' % (idx, kind, '503' if bad else 'OK'))
            if kind == '복귀' and bad and not said_cause:
                # 한 판에서 여러 복귀가 연달아 죽으면 사유는 같다 -- 한 번만 싣는다
                said_cause = True
                print('*** 복귀 503 원인 전문:')
                print('***   %s' % det[-320:])
        moved = (cfg != BASE) or (moved and bad)
    print('조종 쓰기:', ' · '.join(writes) or '없음 (전 시행이 기준선 배치라 쓰기가 없다)')
    print()
    print('종료 사유 집계:', counts)
    # The verdicts live per trial, not in a top-level ``grid`` -- reading the
    # latter always printed "none" even for an episode whose retention
    # qualified at T7 (2026-09-17), which is exactly the sort of quiet
    # misreport that sends the next hour down the wrong hole.
    attained = {}
    for tr in d.get('trials', []):
        for target, row in (tr.get('verdicts') or {}).items():
            if isinstance(row, dict) and row and all(v == 'PASS' for v in row.values()):
                attained.setdefault(target, []).append(tr.get('controlId'))
    if attained:
        for target in sorted(attained):
            print('전부 PASS: %-4s <- %s' % (target, ', '.join(map(str, attained[target]))))
    else:
        print('전부 PASS 인 열: 없음')
    # 저널이 남았는지: 비어 있으면 사후 진단이 불가능하다는 뜻이므로 소리내어 말한다.
    import glob as _g
    root = os.path.dirname(os.path.dirname(path))
    files = _g.glob(os.path.join(root, 'action-r1-state', '*', '*'))
    print('R1 저널 파일 %d개%s' % (len(files),
          '' if files else '  ← 비어 있다: 적용 실패의 사후 진단이 불가능하다'))

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '.')
