"""조종 명령이 실제로 목표 셀에 닿았는지 전수로 센다.

2026-09-23: 같은 질문에 내가 **세 번** 다른 답을 냈다 -- 판 하나만 보고 "HO 가 UE 를
죽인다", KPM 파일 하나만 보고 "45% 성공", 그리고 184개 파일 전수로 "양방향 71%".
틀린 이유는 매번 **표본이 질문의 창을 덮지 않았다**는 것 하나였다.  그래서 판정을
일회성 스크립트가 아니라 여기 둔다.

핵심 함수 `census()` 는 순수하고, 결정적인 규칙은 셋이다:

* **파일 끝 여유(`guard_s`)** -- 명령 뒤 `guard_s` 초가 관측 창 안에 없으면 *판정하지
  않는다*.  이것을 빼면 파일 경계 근처의 모든 명령이 "사라짐" 이 된다(내 2번째 오답).
* **중복 제거** -- 한 명령이 여러 status_event 를 낳으므로 (UE, 초) 로 묶는다.
  안 하면 한 번의 이동이 다섯 번 세어진다.
* **기준선 고정 제외** -- 목표가 이미 현재 셀이면 이동이 아니다.
"""
from __future__ import annotations

import bisect
import collections
import datetime
import glob
import json
import sqlite3
from pathlib import Path

DB = Path('/opt/ran-lab/controller/oran-deploy/session-20260819/state/a1p/a1p-producer.sqlite3')
KPM_GLOB = '/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live/a1-live-kpm.jsonl*'

#: nr_cellid -> E2 nb_id.  configs/oai/*.conf 의 `nr_cellid` 가 근거다
#: ([[a-verdict-frozen-in-a-constant-outlives-its-evidence]] -- 반송파를 고치고
#: 이 표를 안 고쳐 하루를 버린 적이 있다).
NB_OF_NCI = {12345678: 3584, 87654321: 2816}

#: 명령 뒤 이만큼이 관측 창 안에 있어야 판정한다.  사라짐 분포의 최댓값이 107 s 였으므로
#: 120 s 면 진짜 사라짐과 관측 부재를 가른다.
GUARD_S = 120.0

#: 이보다 빨리 마지막 표본이 끊기면 "사라짐", 그보다 길게 살아 있으면 "도달못함(생존)".
VANISH_S = 120.0


def census(commands, observations, *, window, guard_s=GUARD_S):
    """{결과: 건수}, 그리고 사라짐까지 걸린 초들.

    ``commands``     -- (t_us, amf, target_nb) 의 순회 가능체
    ``observations`` -- {amf: [(t_us, nb_id), ...]} (시각 오름차순)
    ``window``       -- (lo_us, hi_us) 관측이 존재하는 구간
    """
    lo, hi = window
    result = collections.Counter()
    vanished = []
    seen = set()
    for t0, amf, target in commands:
        key = (amf, round(t0 / 1e6))
        if key in seen:                       # 한 명령의 중복 상태사건
            continue
        seen.add(key)
        rows = observations.get(amf) or ()
        if not rows or t0 < lo or t0 + guard_s * 1e6 > hi:
            result['창 밖/여유 부족'] += 1
            continue
        times = [t for t, _ in rows]
        index = bisect.bisect_left(times, t0)
        after = rows[index:]
        if not after:
            result['명령 후 표본 0'] += 1
            continue
        source = rows[index - 1][1] if index else None
        if source == target:
            result['이미 목표(기준선)'] += 1
            continue
        leg = f'{source}->{target}'
        if any(nb == target for _, nb in after):
            result[f'{leg} 도달'] += 1
        elif (after[-1][0] - t0) / 1e6 < VANISH_S:
            result[f'{leg} 사라짐'] += 1
            vanished.append(round((after[-1][0] - t0) / 1e6))
        else:
            result[f'{leg} 도달못함(생존)'] += 1
    return result, sorted(vanished)


def agreement(trials, observations, *, window, nb_of_nci=None, guard_s=GUARD_S):
    """커널의 잠금이 **무선의 실제 결과와 일치하는가**.

    ``trials`` -- (t_us, amf, target_nb, locked) 의 순회 가능체.

    2026-09-23 실측: 무선이 목표에 **도달했는데도** 잠긴 비율 50% (18/36),
    못 갔을 때 잠긴 비율 47% (15/32).  **구별이 안 된다** -- 잠금이 실제 결과를
    재고 있지 않다는 뜻이다.  판 수율의 지배 요인은 무선이 아니라 이 검증 경로다.

    주의: 도달=False 쪽은 오염돼 있다.  UE 가 판 도중 재등록하면 조립 시점 id 로는
    찾을 수 없어 '미도달' 로 보인다([[steering-readback-froze-the-composition-time-ue-id]]).
    **도달=True 쪽만 견고하다** -- 낡은 번호로는 목표 셀에 나타날 수 없기 때문이다.
    """
    lo, hi = window
    table = collections.Counter()
    for t0, amf, target, locked in trials:
        rows = observations.get(amf) or ()
        if not rows or t0 < lo or t0 + guard_s * 1e6 > hi:
            table['판정불가(창 밖)'] += 1
            continue
        times = [t for t, _ in rows]
        after = rows[bisect.bisect_left(times, t0):]
        if not after:
            table['판정불가(창 밖)'] += 1
            continue
        reached = any(nb == target for _, nb in after)
        table[('도달' if reached else '미도달', '잠금' if locked else '정상')] += 1
    return table


def read_observations(pattern=KPM_GLOB):
    """{amf: [(t_us, nb_id)]} 과 관측 창.  회전본까지 모두 읽는다."""
    rows = collections.defaultdict(list)
    lo = hi = None
    for path in sorted(glob.glob(pattern)):
        with open(path, errors='replace') as handle:
            for line in handle:
                line = line.strip()
                if not line.startswith('{'):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                at = record.get('recv_unix_us') or 0
                if not at:
                    continue
                lo = at if lo is None or at < lo else lo
                hi = at if hi is None or at > hi else hi
                for ue in record.get('ues') or ():
                    rows[ue.get('amf_ue_ngap_id')].append((at, record.get('nb_id')))
    for amf in rows:
        rows[amf].sort()
    return rows, (lo, hi)


def read_commands(db=DB):
    """조종 정책이 ENFORCED 된 순간들: (t_us, amf, target_nb)."""
    connection = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    scoped = {}
    for row in connection.execute('select policy_id,policy_json from policy_history'):
        try:
            body = json.loads(row['policy_json'])
            body = body.get('policyObject', body)
            cells = body['steeringObjective']['actionEnvelope']['allowedCells']
            scoped[row['policy_id']] = (
                body['scope']['ueId']['guAmfUeNgapId']['amfUeNgapId'],
                NB_OF_NCI.get(cells[0]['cId']['ncI']) if cells else None)
        except (KeyError, TypeError, ValueError):
            continue
    out = []
    for row in connection.execute('select policy_id,created_at,status_json '
                                  'from status_events order by sequence'):
        if row['policy_id'] not in scoped:
            continue
        try:
            if json.loads(row['status_json']).get('enforceStatus') != 'ENFORCED':
                continue
        except ValueError:
            continue
        amf, target = scoped[row['policy_id']]
        at = datetime.datetime.fromisoformat(
            row['created_at'].replace('Z', '+00:00')).timestamp() * 1e6
        out.append((at, amf, target))
    connection.close()
    return out


def main() -> None:
    observations, window = read_observations()
    stamp = lambda us: datetime.datetime.fromtimestamp(
        us / 1e6, datetime.timezone.utc).strftime('%m-%d %H:%M:%S')
    print(f'관측 창 {stamp(window[0])} ~ {stamp(window[1])} UTC')
    result, vanished = census(read_commands(), observations, window=window)
    reached = sum(v for k, v in result.items() if k.endswith('도달'))
    judged = reached + sum(v for k, v in result.items()
                           if k.endswith('사라짐') or k.endswith('도달못함(생존)'))
    for key, value in sorted(result.items(), key=lambda kv: -kv[1]):
        print(f'  {value:5d}  {key}')
    if judged:
        print(f'\n판정된 이동 {judged}건 중 도달 {reached} = {reached / judged:.0%}')
    if vanished:
        print(f'사라지기까지 초: 중앙값 {vanished[len(vanished) // 2]} '
              f'범위 {vanished[0]}~{vanished[-1]} (n={len(vanished)})')


if __name__ == '__main__':
    main()
