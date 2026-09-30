#!/usr/bin/env python3
"""`RESULTS-*.md` 의 제어-효과 표를 판 문서에서 다시 만든다.

손으로 센 수를 문서에 박아 두면 판이 쌓일 때마다 낡는다.  2026-09-17 에 이
계산을 네 번 다시 했고 그 중 두 번은 **요약 단위를 잘못 잡아** 틀렸다.  그래서
규칙을 코드에 둔다:

* 오염은 **무캡 시행의 세 UE 총합**으로 판정한다.  한 UE 값으로 보면 같은 셀을
  나눠 쓰는 둘이 합을 보존한 채 갈린 것(정상 경쟁)을 고장으로 읽는다.
* 제어의 성적은 **타깃 전부 PASS** 로 잰다.  그 제어가 겨냥한 KPI 하나로 재면
  순위가 뒤집힌다 -- `pf@ue2=4.0` 은 단일 KPI 92%, 전부 PASS 31% 다.

사용: python3 control_effects.py [판 디렉터리의 glob]
"""
import json, glob, os, re, statistics, sys
from collections import Counter, defaultdict

CONDITION = "v4-tight90-P1-L8"

#: 분석에 넣을 **팔**.  `AIC_CONDITION` 은 선호와 부하로만 만들어지므로 팔이 달라도
#: 이름이 같다.  2026-09-17 에 monolith 팔을 돌리기 시작했는데 이 도구가 조건만 보면
#: 두 팔이 한 통에 섞여, 오늘까지의 three-agent 결론이 조용히 오염된다.
#: `AIC_ANALYSIS_METHOD=*` 로 전부 보기, 다른 값으로 그 팔만 보기.
METHOD = os.environ.get("AIC_ANALYSIS_METHOD", "three-agent")


def off_condition(doc):
    """조건이 다르거나 **팔이 다르면** 이 판은 이 분석의 것이 아니다."""
    if (doc.get("condition") or {}).get("name") != CONDITION:
        return True
    if METHOD in ("", "*"):
        return False
    return str(doc.get("method") or "three-agent") != METHOD
NOCAP_TOTAL_FLOOR = 17.0          # 건강한 무캡 총합 20.3~24.3, 진짜 붕괴 8~16
DEAD_UE_FLOOR = 1.0               # 자기 축에 캡이 없는데 이 아래면 죽은 것이다
# ①-c 한 UE 만 반토막 났을 때 ② 의 총합은 나머지 둘에 가려 문턱을 넘는다 — 17:53 판은
# ue1 2.25 인데 합 18.33 이라 깨끗으로 셌다.  문턱은 고르지 않고 분포에서 뽑았다:
# 79 개 정상 판(① 이 안 잡는 것만)에서 이 짝이 걸리는 판은 **1 개(1.3%)** 이고,
# ①②가 놓치던 오염 판 4 개 중 **3 개**를 잡는다.  더 낮추면(4.0) 검출이 1 개로 줄고,
# 더 높이면(5.0/6.5) 거짓양성이 2.5%~15% 로 뛴다.
LONE_UE_FLOOR = 4.5               # 이 아래인데
HEALTHY_PEER_FLOOR = 7.0          # 나머지 무캡 UE 가 둘 다 이 위면 그 UE 만 고장이다
DEADLINE_THRESHOLD = 7.83         # 221 시행에서 오류 6건(2.7%); 재최적화해도 7.909
UES = ("ue1", "ue2", "ue3")


def goodputs(trial):
    kp = trial.get("kpis") or {}
    return [kp.get("dlGoodputMbps@%s" % u) for u in UES]


def dirt_reasons(episode):
    """이 판이 UE 하향 장애로 오염된 **사유들**.  비어 있으면 깨끗하다.

    자가 셋이고 **셋 다 필요하다.**

    ① 자기 축에 캡이 없는데 죽어 있는 UE.  캡이 *다른* UE 에 걸렸든 말든 고장이다.
    ①-b **정확히 0.00 은 캡이 있어도 사망**이다.  전 자료에서 가장 센 캡(6 PRB)은
        n=18 에 **최소 2.31** 인데, 더 느슨한 캡 12·18 에서 0 이 나온다(각각 3건·11건,
        전부 정확히 0.00).  느슨한 캡이 센 캡보다 적게 줄 수는 없다.
    ② 캡이 하나도 없는 시행의 세 UE 총합이 바닥 아래 -- 단 **기준선과 같은 셀 배치**
        에서만 읽는다.  조종이 세 UE 를 한 셀로 모으면 총합이 셀 용량으로 눌리는 것이
        맞다(판 021337: 두 셀 19.4~24.4 대 한 셀 15.40).

    ② 만 쓰면 다른 UE 에 캡이 걸린 시행을 통째로 건너뛰어 그 사이에 죽은 UE 를 놓치고
    (판 015412), ① 만 쓰면 아무도 0 은 아닌데 셋이 함께 주저앉은 판을 놓친다.

    **이 함수가 유일한 구현이다.**  2026-09-17 에 같은 규칙이 `judge_episode.py` 와
    여기 두 곳에 각각 있어서, 구멍을 고칠 때마다 두 번 고쳐야 했다 -- 네 번 중 한 번만
    빠뜨려도 판정기와 집계기가 서로 다른 판을 깨끗하다고 부르게 된다.
    """
    def cells(cfg):
        return tuple(cfg.get("servingCell@%s" % u) for u in UES)
    reasons = []
    trials = episode.get("trials", [])
    baseline_cells = cells({k: str(v) for k, v in
                            ((trials[0].get("configuration") or {}) if trials else {}).items()})
    for trial in trials:
        cfg = {k: str(v) for k, v in (trial.get("configuration") or {}).items()}
        g = goodputs(trial)
        index = trial.get("trialIndex")
        if not all(isinstance(x, (int, float)) for x in g):
            continue
        for ue, value in zip(UES, g):                       # ① / ①-b
            uncapped = cfg.get("dlPrbCap@%s" % ue, "0") == "0"
            if value < DEAD_UE_FLOOR and uncapped:
                reasons.append("시행%s %s=%.2f (자기 캡 없음)" % (index, ue, value))
            elif value == 0.0 and not uncapped:
                reasons.append("시행%s %s=0.00 (캡 %s 인데 0 — 캡은 0 을 못 만든다)"
                               % (index, ue, cfg.get("dlPrbCap@%s" % ue)))
        for ue, value in zip(UES, g):                       # ①-c
            # **셀을 혼자 쓰는 UE 에만 쓴다.**  셀을 나눠 쓰는 둘은 합을 보존한 채 갈릴 수
            # 있고(정상 경쟁), 그때 낮은 쪽을 고장으로 읽으면 안 된다 -- 7.7/9.0/4.1 이
            # 바로 그런 판이다.  셀을 모르는 문서도 적용 대상이 아니다(보수적으로 통과).
            mine = cfg.get("servingCell@%s" % ue)
            alone = bool(mine) and not any(cfg.get("servingCell@%s" % u) == mine
                                           for u in UES if u != ue)
            others = [x for u, x in zip(UES, g) if u != ue]
            if (alone and cfg.get("dlPrbCap@%s" % ue, "0") == "0" and value < LONE_UE_FLOOR
                    and all(cfg.get("dlPrbCap@%s" % u, "0") == "0" for u in UES if u != ue)
                    and all(x >= HEALTHY_PEER_FLOOR for x in others)):
                reasons.append("시행%s %s=%.2f (셀 단독인데) 나머지가 %s (그 UE 만 죽은 것)"
                               % (index, ue, value, "/".join("%.2f" % x for x in others)))
        if any(cfg.get("dlPrbCap@%s" % u, "0") != "0" for u in UES):
            continue
        if cells(cfg) != baseline_cells:
            continue
        if sum(g) < NOCAP_TOTAL_FLOOR:                      # ②
            reasons.append("시행%s 합=%.2f %s" % (index, sum(g),
                           {u: round(v, 2) for u, v in zip(UES, g)}))
    return reasons


def is_dirty(episode):
    return bool(dirt_reasons(episode))


#: 배포 토폴로지의 기준 배치.  이것과 다르면 그 시행은 **셀을 옮기라고 요청한** 것이다.
BASE_PLACEMENT = {"servingCell@ue1": "87654321",
                  "servingCell@ue2": "12345678",
                  "servingCell@ue3": "12345678"}


def control_label(cfg):
    """한 시행을 제어 한 이름으로 부른다 (비기본 축이 하나라는 전제).

    조종(셀 이동)도 이름을 받아야 한다.  2026-09-17 에 이 함수가 `servingCell` 을
    아예 보지 않아서, **셀을 옮기라고 요청한 시행 16건이 "기준" 으로 집계**되고
    있었다(91건 중 18%).  그중 14건은 09-16 인데, 그날 조종은 137번 요청되고
    **한 번도 일어나지 않았다**(AMF Ack 0건, RESULTS 4.1) -- 효과로는 무해했지만
    이름으로는 거짓이고, 조종이 실제로 듣는 날에는 기준선을 오염시킨다.
    """
    placement = {k: v for k, v in cfg.items() if k.startswith("servingCell")}
    if placement and placement != BASE_PLACEMENT:
        moved = sorted(k.split("@")[1] for k, v in placement.items()
                       if BASE_PLACEMENT.get(k) != v)
        return "steer@" + ",".join(moved)
    if cfg.get("pfWeight@ue2") == "4.0":
        return "pf@ue2=4.0"
    if cfg.get("pfWeight@ue3") == "4.0":
        return "pf@ue3=4.0"
    if cfg.get("dlPrbCap@ue3", "0") != "0":
        return "cap@ue3=%s" % cfg["dlPrbCap@ue3"]
    if cfg.get("dlPrbCap@ue2", "0") != "0":
        return "cap@ue2=%s" % cfg["dlPrbCap@ue2"]
    return "기준"


def collect(pattern):
    rows = defaultdict(list)
    kept = skipped = 0
    for path in sorted(glob.glob(pattern)):
        doc = json.load(open(path))
        if off_condition(doc):
            continue
        if is_dirty(doc):
            skipped += 1
            continue
        kept += 1
        for trial in doc.get("trials", []):
            cfg = {k: str(v) for k, v in (trial.get("configuration") or {}).items()}
            g = goodputs(trial)
            if not all(isinstance(x, (int, float)) for x in g):
                continue
            verdicts = trial.get("verdicts") or {}
            attained = any(isinstance(r, dict) and r and all(v == "PASS" for v in r.values())
                           for r in verdicts.values())
            rows[control_label(cfg)].append({
                "ue2": g[1], "ue3": g[2], "pool": g[1] + g[2],
                "crossed": g[1] >= DEADLINE_THRESHOLD, "attained": attained})
    return rows, kept, skipped


def table(rows, key, title):
    print("\n%s" % title)
    print("  %-14s %4s  %-14s %-9s %s" % ("제어", "n", "비율", "ue3 중앙", "풀 중앙"))
    for label in sorted(rows, key=lambda x: -sum(r[key] for r in rows[x]) / max(len(rows[x]), 1)):
        v = rows[label]
        hit = sum(r[key] for r in v)
        print("  %-14s %4d  %3d/%-3d %4.0f%%  %-9.2f %.2f"
              % (label, len(v), hit, len(v), 100 * hit / len(v),
                 statistics.median(r["ue3"] for r in v),
                 statistics.median(r["pool"] for r in v)))


def target_ladder(pattern):
    """요구조건별·타깃별 통과율.  양보를 키울수록 달성 가능해지는지 본다.

    세는 단위는 **(시행 × 타깃 × 요구조건)** 조합이다.  판도 시행도 아니다 --
    2026-09-17 에 단위를 다섯 번 헷갈렸으므로 여기 적어 둔다.
    """
    from collections import Counter
    ok_req, tot_req = Counter(), Counter()
    ok_tgt, tot_tgt = Counter(), Counter()
    for path in sorted(glob.glob(pattern)):
        doc = json.load(open(path))
        if off_condition(doc) or is_dirty(doc):
            continue
        for trial in doc.get("trials", []):
            for target, row in (trial.get("verdicts") or {}).items():
                if not isinstance(row, dict):
                    continue
                for req, verdict in row.items():
                    tot_req[req] += 1; tot_tgt[target] += 1
                    if verdict == "PASS":
                        ok_req[req] += 1; ok_tgt[target] += 1
    if not tot_req:
        print("판정된 조합이 없다:", pattern)
        return 1
    print("요구조건별 (조합 %d개)" % sum(tot_req.values()))
    for k in sorted(tot_req, key=lambda x: -ok_req[x] / tot_req[x]):
        print("  %-10s %5d/%-6d %5.1f%%" % (k, ok_req[k], tot_req[k],
                                            100 * ok_req[k] / tot_req[k]))
    print("\n타깃별 — 위가 양보가 큰 쪽이면 사다리가 작동하는 것이다")
    for k in sorted(tot_tgt, key=lambda x: -ok_tgt[x] / tot_tgt[x]):
        print("  %-6s %5d/%-6d %5.1f%%" % (k, ok_tgt[k], tot_tgt[k],
                                           100 * ok_tgt[k] / tot_tgt[k]))
    return 0


#: 타깃별 통과율 = "양보 정도" 를 한 수로 세운 것.  타깃 id 는 자리번호라 크기
#: 비교가 안 되므로([[target-ids-are-positional]]) 이 수가 필요하다.
#:
#: **자료에서 계산한다.**  2026-09-17 17:05 에 손으로 재어 박아 뒀는데, 같은 날
#: 저녁에 다시 재니 T7 이 68.4 → 71.0, T8 이 67.8 → 72.8 로 움직여 있었다.
#: 이 사전은 `--negotiation` 의 모든 수를 떠받치므로, 낡으면 그 절이 통째로 낡는다.
_TARGET_EASE_CACHE = {}


def target_ease(pattern):
    """타깃별 요구조건 통과율(%).  판이 쌓이면 자동으로 따라간다."""
    if pattern in _TARGET_EASE_CACHE:
        return _TARGET_EASE_CACHE[pattern]
    rows = defaultdict(lambda: [0, 0])
    for path in sorted(glob.glob(pattern)):
        doc = json.load(open(path))
        if off_condition(doc) or is_dirty(doc):
            continue
        for trial in doc.get("trials", []):
            for target, row in (trial.get("verdicts") or {}).items():
                if not isinstance(row, dict) or not row:
                    continue
                for verdict in row.values():
                    rows[target][1] += 1
                    rows[target][0] += (verdict == "PASS")
    ease = {t: 100.0 * ok / total for t, (ok, total) in rows.items() if total}
    _TARGET_EASE_CACHE[pattern] = ease
    return ease


def negotiation(pattern):
    """에이전트가 실패할수록 양보를 더 키우는가.  세는 단위는 **시행**이다."""
    from collections import defaultdict
    by_index = defaultdict(list)
    distinct_t, distinct_c = [], []
    for path in sorted(glob.glob(pattern)):
        doc = json.load(open(path))
        if off_condition(doc) or is_dirty(doc):
            continue
        seq = [t.get("proposedTargetId") for t in doc.get("trials", [])
               if t.get("proposedTargetId")]
        if not seq:
            continue
        distinct_t.append(len(set(seq)))
        distinct_c.append(len({t.get("controlId") for t in doc.get("trials", [])
                               if t.get("controlId")}))
        ease = target_ease(pattern)
        for index, target in enumerate(seq):
            if target in ease:
                by_index[index].append(ease[target])
    if not by_index:
        print("제안 기록이 없다:", pattern)
        return 1
    print("시행 순서별 — 제안 타깃의 평균 달성 용이도")
    for index in sorted(by_index):
        rows = by_index[index]
        if len(rows) >= 5:
            print("  시행 %d: %5.1f%%  (n=%d)" % (index, statistics.mean(rows), len(rows)))
    print("\n판당 서로 다른 타깃 중앙 %.0f · 서로 다른 제어 중앙 %.0f"
          % (statistics.median(distinct_t), statistics.median(distinct_c)))
    print("시행 0 에서 1 로 크게 뛰고 그 뒤가 평평하면 '한 번 양보하고 머문다' 이다.")
    return 0


def cap_strength(pattern):
    """캡 세기별 ue2 굿풋과 **세 UE 총합**.  세는 단위는 **시행**이다.

    좋은 제어가 좋은 이유를 여기서 본다: 가벼운 캡(18 PRB)은 캡 받은 UE 를 조금만
    낮추면서 **총합을 올린다**.  세게 조이면(6) 둘 다 무너진다.
    """
    from collections import defaultdict
    rows = defaultdict(list)
    for path in sorted(glob.glob(pattern)):
        doc = json.load(open(path))
        if off_condition(doc) or is_dirty(doc):
            continue
        for trial in doc.get("trials", []):
            cfg = {k: str(v) for k, v in (trial.get("configuration") or {}).items()}
            g = goodputs(trial)
            if not all(isinstance(x, (int, float)) for x in g):
                continue
            moved = {k: v for k, v in cfg.items()
                     if (k.startswith("dlPrbCap") and v != "0")
                     or (k.startswith("pfWeight") and v not in ("1.0", "1"))}
            if not moved:
                rows["기준선"].append((g[1], sum(g)))
            elif list(moved) == ["dlPrbCap@ue2"]:
                rows["cap@ue2=%s" % moved["dlPrbCap@ue2"]].append((g[1], sum(g)))
    if not rows:
        print("시행이 없다:", pattern)
        return 1
    print("제어           n    ue2 중앙   총합 중앙   ue2 최대")
    for key in sorted(rows, key=lambda k: -statistics.median(x[1] for x in rows[k])):
        v = rows[key]
        print("  %-12s %3d  %7.2f   %7.2f   %6.2f"
              % (key, len(v), statistics.median(x[0] for x in v),
                 statistics.median(x[1] for x in v), max(x[0] for x in v)))
    print("\n총합이 가장 큰 줄이 기준선이 아니면, **가벼운 캡이 전체를 올린다**는 뜻이다.")
    return 0


def retention(pattern):
    """유지 자격을 **무엇을 유지했는지**로 갈라 센다.  세는 단위는 **판**이다.

    섞어 세면 "유지 자격 N%" 가 조정 성능처럼 보이는데, 실제로는 기준선을
    되돌리는 판이 몇이었는지를 반영한다 (2026-09-17: 기준선 11% 대 제어 46%).
    """
    from collections import Counter
    got, total, tried, skipped = Counter(), Counter(), Counter(), Counter()
    reasons = Counter()
    for path in sorted(glob.glob(pattern)):
        doc = json.load(open(path))
        if off_condition(doc):
            continue
        held = doc.get("retained") or {}
        control = str(held.get("controlId") or "")
        if not control:
            continue
        kind = "기준선 C0" if control.upper() == "C0" else "제어 (비-C0)"
        total[kind] += 1
        if held.get("qualified"):
            got[kind] += 1; tried[kind] += 1
            continue
        # 상위 detail 이 "이미 걸려 있다" 면 **시도조차 필요 없었던** 것이다.
        # 그것을 실패로 세면 분모가 오염된다 (2026-09-17 에 한 번 그렇게 셌다).
        top = str(held.get("detail") or "")
        if "already holds it" in top:
            skipped[kind] += 1
            continue
        detail = str(((held.get("trial") or {}).get("kernel") or {}).get("detail") or "")
        tried[kind] += 1
        reasons[(kind,
                 "fence 409" if "fencingToken" in detail
                 else "철회/적용 실패" if any(x in detail for x in
                                             ("PARTIAL_APPLY", "REJECTED", "UNKNOWN"))
                 # 센티널 복귀를 **표현할 허가증이 없다** -- 철회 실패와 같은 뿌리다.
                 else "센티널 복귀 불가" if "no permit expresses" in top or "left live on" in top
                 else "복구 사고로 차단" if "recovery incident" in top
                 else "기타")] += 1
    if not total:
        print("유지 기록이 없다:", pattern)
        return 1
    print("유지 대상        자격/시도   비율    (시도 불필요)")
    for kind in sorted(total):
        n = tried[kind] or 1
        print("  %-14s %3d/%-4d  %5.0f%%   %d" % (kind, got[kind], tried[kind],
                                                  100 * got[kind] / n, skipped[kind]))
    print("\n실패 사유")
    for key, count in sorted(reasons.items()):
        print("  %-14s %-16s %d" % (key[0], key[1], count))
    print("\n두 줄의 비율이 크게 다르면 이 수치를 합쳐서 보고하면 안 된다.")
    _retention_by_era(pattern)
    return 0


#: A1 쓰기 타임아웃을 20초로 올린 판.  그 앞뒤로 유지의 성격이 달라진다.
RETENTION_ERA_BOUNDARY = "20260916T215918"


def _retention_by_era(pattern):
    """유지의 결말을 수정 전후로 가른다.

    **왜 갈라야 하나**: 수정 이전에는 기전이 거의 완주하지 못해서(14판 중 1판)
    "효과가 재현되는가" 를 **잴 수조차 없었다**.  합쳐 세면 그 사실이 사라지고
    재현율이 낮아 보일 뿐이다.
    """
    from collections import Counter
    tally = Counter()
    for path in sorted(glob.glob(pattern)):
        doc = json.load(open(path))
        if off_condition(doc):
            continue
        stamp = re.search(r"formal38guarded-(\d{8}T\d{6})", path)
        if not stamp:
            continue
        era = "수정 이후" if stamp.group(1) >= RETENTION_ERA_BOUNDARY else "수정 이전"
        held = doc.get("retained") or {}
        if not held.get("controlId"):
            continue
        top = str(held.get("detail") or "")
        kernel = (held.get("trial") or {}).get("kernel") or {}
        detail = str(kernel.get("detail") or "")
        if "already holds it" in top:
            state = "시도 불필요"
        elif held.get("qualified"):
            state = "재현 성공"
        elif detail == "finalized live" or kernel.get("outcome") == "SUCCESS":
            state = "재현 실패"
        else:
            state = "기전 실패"
        tally[(era, state)] += 1
    print("\n결말을 수정 전후로 가르면")
    for era in ("수정 이전", "수정 이후"):
        total = sum(v for k, v in tally.items() if k[0] == era)
        if not total:
            continue
        ran = tally[(era, "재현 성공")] + tally[(era, "재현 실패")]
        print("  %s (유지 %d판)  기전 실패 %d(%.0f%%) · 완주 %d판 중 재현 %d(%s)"
              % (era, total, tally[(era, "기전 실패")],
                 100 * tally[(era, "기전 실패")] / total, ran,
                 tally[(era, "재현 성공")],
                 "%.0f%%" % (100 * tally[(era, "재현 성공")] / ran) if ran else "잴 수 없음"))


def main(pattern):
    rows, kept, skipped = collect(pattern)
    if not rows:
        print("해당 조건의 판이 없다:", pattern)
        return 1
    print("조건 %s · 깨끗한 판 %d개 (오염 %d개 제외) · 시행 %d개"
          % (CONDITION, kept, skipped, sum(len(v) for v in rows.values())))
    table(rows, "crossed", "(a) 단일 KPI — ue2 굿풋 >= %.2f Mbps" % DEADLINE_THRESHOLD)
    table(rows, "attained", "(b) 판정 기준 — **타깃 전부 PASS**")
    print("\n두 표의 1위가 다르면 그것이 조정 문제의 증거다.")
    return 0


def _self_check():
    """합이 보존된 채 갈린 시행은 오염이 아니고, 무너진 총합은 오염이다."""
    split = {"trials": [{"configuration": {}, "kpis": {
        "dlGoodputMbps@ue1": 7.7, "dlGoodputMbps@ue2": 9.0, "dlGoodputMbps@ue3": 4.1}}]}
    dead = {"trials": [{"configuration": {}, "kpis": {
        "dlGoodputMbps@ue1": 7.9, "dlGoodputMbps@ue2": 0.0, "dlGoodputMbps@ue3": 7.9}}]}
    assert not is_dirty(split), "합 20.8 은 정상 경쟁이다"
    assert is_dirty(dead), "합 15.8 은 UE 사망이다"
    capped = {"trials": [{"configuration": {"dlPrbCap@ue2": "6"}, "kpis": {
        "dlGoodputMbps@ue1": 7.9, "dlGoodputMbps@ue2": 2.5, "dlGoodputMbps@ue3": 6.0}}]}
    assert not is_dirty(capped), "캡이 걸린 시행은 총합이 줄어드는 게 정상이다"
    # 판 015412 의 모양: 캡은 ue2 에 걸렸는데 죽은 것은 ue3 다.
    other = {"trials": [{"configuration": {"dlPrbCap@ue2": "12"}, "kpis": {
        "dlGoodputMbps@ue1": 7.0, "dlGoodputMbps@ue2": 6.5, "dlGoodputMbps@ue3": 0.0}}]}
    assert is_dirty(other), "자기 캡이 없는데 0.00 이면 다른 UE 의 캡과 무관하게 고장이다"
    print("자체 검사 통과")



def by_era(pattern):
    """(a)/(b) 를 **시대로 갈라** 본다.

    2026-09-17 에 배운 것: 이 자료는 수정 전후로 거의 모든 지표가 갈리므로,
    통제하지 않은 비교는 대부분 시대를 다시 재는 것이다 (RESULTS 6).  그러니
    1 의 역전(단일 KPI 1위가 판정 기준에서는 바닥)이 **시대 artifact 가
    아닌지** 물어야 한다.  답은 아니다 -- 두 시대 모두에서 같은 방향이다.
    """
    rows = defaultdict(Counter)
    for path in sorted(glob.glob(pattern)):
        doc = json.load(open(path))
        if off_condition(doc) or is_dirty(doc):
            continue
        era = path.split("formal38guarded-")[1][:8]
        for trial in doc.get("trials", []):
            cfg = {k: str(v) for k, v in (trial.get("configuration") or {}).items()}
            ue2 = (trial.get("kpis") or {}).get("dlGoodputMbps@ue2")
            if not isinstance(ue2, (int, float)):
                continue
            verdicts = trial.get("verdicts") or {}
            row = rows[(era, control_label(cfg))]
            row["n"] += 1
            row["kpi"] += ue2 >= 7.83   # 2 의 실측 마감 문턱
            row["pass"] += any(isinstance(r, dict) and r
                               and all(x == "PASS" for x in r.values())
                               for r in verdicts.values())
    if not rows:
        # 빈 표를 내면 "차이가 없다" 로 읽힌다.  없는 것과 찾지 못한 것은 다르다.
        print("해당 조건의 판이 없다: %s" % pattern)
        return 1
    print("조건 %s · 시대별" % CONDITION)
    print("%-13s %-9s %4s %9s %13s" % ("제어", "시대", "n", "(a) KPI", "(b) 전부PASS"))
    eras = sorted({era for era, _ in rows})
    for label in sorted({lab for _, lab in rows}):
        for era in eras:
            row = rows[(era, label)]
            if not row["n"]:
                continue
            print("%-13s %-9s %4d %7.0f%% %12.0f%%"
                  % (label, era, row["n"], 100 * row["kpi"] / row["n"],
                     100 * row["pass"] / row["n"]))
        print()
    print("1 의 역전이 두 시대 모두에서 같은 방향이면 시대 artifact 가 아니다.")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--self-check":
        _self_check()
        raise SystemExit(0)
    mode = args[0] if args and args[0].startswith("--") else None
    if mode:
        args = args[1:]
    _self_check()
    # 기본 패턴은 **이 파일 위치**에 붙인다.  cwd 에 붙여 두었더니 다른
    # 디렉터리에서 부를 때마다 "판이 없다" 고 조용히 거짓말했다.
    pattern = args[0] if args else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "formal38guarded-2026*T*/evidence/AGENT-*-episode.json")
    raise SystemExit({"--targets": target_ladder,
                      "--negotiation": negotiation,
                      "--retention": retention,
                      "--caps": cap_strength,
                      "--era": by_era}.get(mode, main)(pattern))
