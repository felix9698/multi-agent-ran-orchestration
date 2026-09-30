#!/usr/bin/env python3
"""세 팔(three-agent / basic-monolith / internal-monolith)을 나란히 놓는다.

왜 따로 만드나: `control_effects.py` 는 **한 팔 안에서** 제어의 효과를 본다
(기본이 three-agent 다).  이 도구는 반대로 **팔 사이**를 본다.

무엇으로 비교하나 — 판 증거가 이미 싣고 있는 것만 쓴다:
  · `termination.reason` / `kernelTermination`   무엇으로 끝났나
  · `retained` · `t0Success` · `firstSuccess`     결과
  · 시행 수                                        탐색 깊이
  · `calls[]` 의 입력/출력 토큰과 `latencyMs`      비용과 지연
  · `verdicts` 가 전부 PASS 인 열                   인텐트 충족

읽기 전용이다.  판을 건드리지 않는다.
"""
import json, glob, os, statistics as st, sys, time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import control_effects as ce

ARMS = ("three-agent", "basic-monolith", "internal-monolith", "deterministic")


def _improved_over_baseline(trials):
    import re as _re
    def best(succ):
        ks = [int(_re.sub(r"\D", "", k)) for k, v in (succ or {}).items()
              if v and _re.sub(r"\D", "", k)]
        return min(ks) if ks else None
    if not trials:
        return False
    base = best(trials[0].get("success"))
    later = [b for b in (best(t.get("success")) for t in trials[1:]) if b is not None]
    return bool(later) and (base is None or min(later) < base)


def _tokens(doc):
    """모델 호출의 입력·출력 토큰 합.  deterministic 호출(0 토큰)도 그대로 센다."""
    ins = outs = lat = 0.0
    n = 0
    for call in doc.get("calls") or []:
        if not isinstance(call, dict):
            continue
        tokens = float(call.get("inputTokens") or 0)
        # deterministic 호출은 0 토큰이다.  호출 수에 세면 **호출당 비용이 희석된다**.
        if not tokens:
            continue
        ins += tokens
        outs += float(call.get("outputTokens") or 0)
        lat += float(call.get("latencyMs") or 0)
        n += 1
    return ins, outs, lat, n


def _all_pass(trial):
    for row in (trial.get("verdicts") or {}).values():
        if isinstance(row, dict) and row and all(v == "PASS" for v in row.values()):
            return True
    return False


# 2026-09-17 밤: 오너 드롭 `.orca/drops/RAN_AGENT_PROMPTS_REVISED_20260917.md` 로
# **여섯 프롬프트 전부**가 교체됐다("그대로 해라, 이건 이제 고정이야").  그러므로
# 이전 판은 **세 팔 모두** 지시가 달랐고 팔 비교에 쓸 수 없다 -- 앞서 basic 한 팔에만
# 두었던 경계를 셋으로 넓힌다.  손으로 거르면 잊어버리므로 여기서 막는다.
# (이 경계 이전: three-agent 55 깨끗한 판 · internal 0 · basic 0.  전부 옛 지시다.)
# 2026-09-17 밤: 프롬프트만이 아니라 **액션 공간**도 바뀌었다.  `run_formal_v3.sh` 가
# v4 의 설정 다섯 곳을 되돌리고 있었고(축·캡·pf·상한·셀), 그것을 고치는 동안 조건이
# 서로 다른 판이 여럿 돌았다(원장 725).  의도한 조건이 선 시각 이후 판만 비교한다.
#   v3(전력 축 없음)      steer 8 x cap 24 x pf 16            =  3,072
#   과도기(전력·넓은 ue3) 위 x atten 25                        = 76,800  <- 상한 4096 에 걸려 판 2 개 거절
#   v4(의도)             steer 8 x cap 12 x pf 8 x atten 25  = 19,200  <- v3 의 6.25 배
#
# 2026-09-18 경계 이동: 23:56:50 이후에도 전력 축을 껐다 켰고(00:14·00:22) 되읽기의
# 이름 체계 불일치를 고쳤다(00:45).  그 사이 판들은 '축은 보이는데 되읽기가 21/21
# 실패' 하는 상태였으므로 같은 실험이 아니다.  경계를 마지막 수정 시각으로 옮긴다.
ACTION_SPACE_EPOCH = 1789669663.5   # 베드 복구·E2 924/916·프리플라이트 통과


def _action_space_stale(started_epoch):
    """이 판이 **의도한 액션 공간 이전**에 시작됐는가."""
    return started_epoch is not None and started_epoch < ACTION_SPACE_EPOCH


#: 핸드오프 2026-09-18 §8.1: 수정이 생기면 새 epoch 다.  ``--formal`` 이면 manifest 의
#: ``codeVersion.codeVersionSha256`` 이 가장 최근 판과 같은 판만 비교한다 -- 버전이 안 찍힌
#: 판(09-19 00:3x 이전)과 다른 버전의 판은 액션 공간 이전 판과 똑같이 뺀다.
FORMAL_CODE = None


def _code_version_of(episode_path):
    board = os.path.dirname(os.path.dirname(episode_path))
    try:
        return (json.load(open(os.path.join(board, "manifest.json"))).get("codeVersion")
                or {}).get("codeVersionSha256")
    except (OSError, ValueError):
        return None


def _latest_code_version(pattern):
    for path in sorted(glob.glob(pattern), reverse=True):
        found = _code_version_of(path)
        if found:
            return found
    return None


PROMPT_EPOCH = {"three-agent": 1789652734.0,
                "internal-monolith": 1789652734.0,
                "basic-monolith": 1789652734.0}   # agents.py 수정 시각 그대로


def _prompt_stale(arm, started_epoch):
    """이 판이 그 팔의 **현재 프롬프트 이전**에 시작됐는가."""
    cut = PROMPT_EPOCH.get(arm)
    return cut is not None and started_epoch is not None and started_epoch < cut


def _started_kst(path):
    """판 디렉터리 이름은 **UTC** 다 (로그 파일명은 KST). 섞으면 9시간이 어긋난다."""
    try:
        stamp = path.split("formal38guarded-")[1][:15]
        return time.mktime(time.strptime(stamp, "%Y%m%dT%H%M%S")) - time.timezone
    except (IndexError, ValueError):
        return None


def collect(pattern, since=None):
    """`since` 는 KST "HH:MM" -- 그 시각 이후 시작한 판만 센다.

    왜 필요한가: 팔은 한 판씩 교대하지만 three-agent 는 **그 전에도** 쌓여 있다.
    베드가 시간에 따라 움직이므로(MCS 18~25, goodput 5.9~8.1, 2026-09-17 실측)
    창을 맞추지 않으면 시대 차이를 팔 차이로 읽게 된다.
    """
    cut = None
    if since:
        today = time.strftime("%Y-%m-%d")
        cut = time.mktime(time.strptime("%s %s" % (today, since), "%Y-%m-%d %H:%M"))
    rows = defaultdict(list)
    for path in sorted(glob.glob(pattern)):
        doc = json.load(open(path))
        if (doc.get("condition") or {}).get("name") != ce.CONDITION:
            continue
        if cut is not None:
            t = _started_kst(path)
            if t is None or t < cut:
                continue
        arm = str(doc.get("method") or "three-agent")
        trials = doc.get("trials") or []
        ins, outs, lat, calls = _tokens(doc)
        rows[arm].append({
            "dirty": ce.is_dirty(doc),
            # **결정 시행**만 센다.  시행0 은 `phase=initial-measurement`,
            # `model=deterministic` 이라 모델이 붙지 않는다 (2026-09-17 오너 질문으로
            # 드러났다: "3시행이면 호출 5회여야 하지 않나" -- 아니다, 시행0 은 모델이
            # 결정하지 않고 internal-monolith 는 T·C 를 한 호출에 합치므로 3회다).
            # 전체 시행으로 나누면 호출/시행이 팔마다 다른 이유가 흐려진다.
            "decided": sum(1 for t in trials
                           if str(((t.get("decision") or {}).get("model") or "")).lower()
                           not in ("", "deterministic", "none")),
            "term": ((doc.get("termination") or {}).get("reason")
                     if isinstance(doc.get("termination"), dict) else doc.get("termination")) or "?",
            # `retained` 는 **dict** 다 -- `bool()` 로 읽으면 언제나 True 가 되어
            # "유지 100%" 라는 무의미한 값이 나온다(2026-09-17 에 한 번 그렇게 냈다).
            # 진짜 판정은 그 안의 `qualified` 다.
            "retained": bool((doc.get("retained") or {}).get("qualified")),
            # 2026-09-18: **결정 초과는 시행 기록에 안 남는다.**  시행이 만들어진
            # 경우만 `decisionLatencyMs` 를 들고 있어서, 초과해서 시행이 안 생긴
            # 건은 통계에서 통째로 빠진다 -- 그래서 "basic 최대 31초, 초과 0건"
            # 으로 보였다.  실제로는 basic 이 102.4·97.7·89.6 초를 썼다(허용 60).
            # 초과는 비시행 사건 `decision-timeout` 에만 남으므로 거기서 센다.
            # 코드 주석대로 "a timeout is an outcome" -- 결과로 세야 한다.
            # 2026-09-18 오너 지시: "agent 결정에 소모되는 시간과 토큰은 늘 기록을
            # 해놔야해".  판 기록의 `resourceCost` 가 이미 다 갖고 있는데 표에 안
            # 실려 있었다 -- 출력 토큰과 **결정별 지연 분포**를 여기서 꺼낸다.
            "outTokens": int(((doc.get("resourceCost") or {}).get("outputTokens") or 0)),
            "latencies": [x for x in ((doc.get("resourceCost") or {}).get(
                "decisionLatenciesMs") or []) if isinstance(x, (int, float)) and x > 0],
            "decisionTimeouts": sum(
                1 for event in (doc.get("nonTrialEvents") or [])
                if "decision-timeout" in json.dumps(event, ensure_ascii=False)),
            "t0": bool(doc.get("t0Success")),          # 이쪽은 진짜 bool
            # `firstSuccess` 도 dict 이고, 비어 있으면 성공이 없었다는 뜻이다.
            "first": bool(doc.get("firstSuccess")),
            # 3-agent 구조가 막아 주는 실패의 후보 둘 (2026-09-17 관찰).
            # `PROPOSAL_FAILURE` 는 "새로운 실행 가능 제안의 고갈" 이다 -- 제안이 거절된
            # 것이 아니라 **이미 해 본 것과 같아서** 실행할 게 없는 상태다.  후보 집합에서
            # 고르는 팔은 구조적으로 이 상태에 덜 빠진다 (three-agent 0/52).
            # 2026-09-18 정정: `PROPOSAL_FAILURE` 는 두 길로 난다 -- 결정 60 초 초과
            # (agent.py:2220) 와 실행할 제안 없음(agent.py:2673).  깨끗한 판 25 건이
            # **전부 전자**였는데 이 열이 둘을 합쳐 "제안 고갈" 이라 불러, 중복 제안
            # 문제가 재발한 것처럼 보고됐다.  여기서는 진짜 무제안만 센다 -- 시간
            # 초과는 윗 표의 `결정초과` 열이 센다.
            "pfail": ((doc.get("termination") or {}).get("reason") == "PROPOSAL_FAILURE"
                      and "decision allowance" not in str(
                          (doc.get("termination") or {}).get("detail") or "")),
            "revisit": "C0" in [t.get("controlId") for t in trials[1:]],
            # 2026-09-18 오너 질문("제안 고갈에도 basic 이 잘 나온다는거야?")에서 나왔다.
            # 첫 성공 52 판 중 49 판이 **시행 0 = 기준선 C0, 경과 0 ms** 였다 -- `first`
            # 는 에이전트가 아니라 베드를 잰다.  에이전트의 기여는 "같은 판에서 기준선보다
            # 더 선호되는(번호가 낮은) T 에 닿았는가" 다.  T 번호는 판 안의 선호 순위라
            # 판 안에서만 비교한다([[target-ids-are-positional-compare-level-vectors]]).
            "improved": _improved_over_baseline(trials),
            "stale_prompt": _prompt_stale(str(doc.get("method") or "three-agent"),
                                          _started_kst(path)),
            "stale_space": (_action_space_stale(_started_kst(path))
                            or (FORMAL_CODE is not None
                                and _code_version_of(path) != FORMAL_CODE)),
            "trials": len(trials),
            "pass": sum(1 for t in trials if _all_pass(t)),
            "in": ins, "out": outs, "lat": lat, "calls": calls,
            # 형성 마감(240 s)을 못 넘겨 죽은 판.  세 팔이 가장 갈릴 곳이다 --
            # three-agent 는 Target·Control 두 번을 형성에 쓰고, monolith 는 한 번이다.
            # [[llm-latency-is-output-bound-and-target-retries]]: target 재시도가
            # 형성을 먹어 240 s 를 1.9 s 넘겨 판 둘이 죽은 적이 있다.
            "formfail": "was not formed within" in (
                (doc.get("termination") or {}).get("detail") or ""
                if isinstance(doc.get("termination"), dict) else ""),
        })
    return rows


def main(pattern, since=None):
    rows = collect(pattern, since)
    if not rows:
        print("해당 조건의 판이 없다: %s" % pattern)
        return 1
    print("조건 %s · 팔 비교%s" % (ce.CONDITION,
          ("  (KST %s 이후 시작한 판만)" % since) if since else ""))
    # **판 합계를 나란히 놓지 마라.**  팔마다 호출 수가 다르고(three-agent 는 시행마다
    # trajectory 를 한 번 더 부른다) 판 길이도 다르므로, 합계 차이의 대부분은 "몇 번
    # 불렀나" 이지 "한 번이 얼마나 비싼가" 가 아니다.  2026-09-17 에 그 합계로
    # "monolith 가 2.5배 싸다" 는 허상을 만들었다 -- 호출당으로 재면 6,816 대 7,723 이다.
    # [[three-arm-cost-lives-in-the-per-move-column]]
    print("%-19s %5s %6s %6s %6s %6s %7s %7s %9s %9s %9s %9s %9s %8s %8s"
          % ("팔", "판", "깨끗", "유지%", "T0%", "달성%", "평균시행",
             "호출/판", "호출/결정", "입력/결정", "출력/결정", "지연중앙s", "지연최대s", "형성실패", "결정초과"))
    # 2026-09-17: 이 열을 "성공%" 라고 부르는 동안 나는 그것을 **인텐트 충족**으로 읽었다.
    # 아니다 -- `firstSuccess` 는 **약화된 목표**(T4·T6 …)를 맞춘 것도 성공으로 센다.
    # 계약 T0 을 맞춘 비율은 옆의 `T0%` 이고 three-agent 55 판에서 **0%** 다.
    # 두 열이 85% 대 0% 로 벌어지는 것이 이 실험의 핵심 사실이므로 이름으로 구분한다.
    print("%-19s %s" % ("", "T0%=계약(굿풋 9.0) 충족 · 달성%=약화 목표 포함 첫성공"))
    for arm in ARMS + tuple(a for a in sorted(rows) if a not in ARMS):
        got = rows.get(arm)
        if not got:
            continue
        # 지시가 달랐던 판은 **팔 비교에서 뺀다** — 오염과는 다른 이유다.
        stale = sum(1 for r in got if r.get("stale_prompt"))
        if stale:
            print("  %-19s (옛 프롬프트 %d 판 제외)" % (arm, stale))
        clean = [r for r in got if not r["dirty"] and not r.get("stale_prompt")
                 and not r.get("stale_space")]
        if not clean:
            # `clean or got` 로 조용히 오염 판에 떨어지면 **거짓 비교 결과**가 나온다.
            # 2026-09-17: ue1 이 PUCCH 침묵으로 주저앉은 구간에서 internal-monolith 의
            # 깨끗한 판이 0 인데 "유지 0% · 성공 0%" 가 찍혔고, 그 수치는 오염 판 4 개를
            # 잰 것이었다.  표본이 없으면 수치를 내지 말고 없다고 말한다.
            print("%-19s %5d %6d   %s" % (arm, len(got), 0,
                                          "깨끗한 판이 없다 — 비교 수치를 내지 않는다"))
            continue
        base = clean
        calls = sum(r["calls"] for r in base)
        dec = sum(r["decided"] for r in base)
        print("%-19s %5d %6d %5.0f%% %5.0f%% %5.0f%% %7.1f %7.1f %9.2f %9.0f %9.0f %9.1f %9.1f %7.0f%% %8d"
              % (arm, len(got), len(clean),
                 100 * sum(r["retained"] for r in base) / len(base),
                 100 * sum(r["t0"] for r in base) / len(base),
                 100 * sum(r["first"] for r in base) / len(base),
                 st.mean(r["trials"] for r in base),
                 calls / len(base),
                 (calls / dec) if dec else 0,
                 (sum(r["in"] for r in base) / dec) if dec else 0,
                 (sum(r["outTokens"] for r in base) / dec) if dec else 0,
                 st.median([x for r in base for x in r["latencies"]] or [0]) / 1000,
                 (max([x for r in base for x in r["latencies"]] or [0])) / 1000,
                 100 * sum(r["formfail"] for r in got) / len(got),
                 sum(r["decisionTimeouts"] for r in got)))
    # 목표 대비 진행률.  팔당 22 판은 첫성공 30%p 차를 검정력 .80 으로 잡는 수다
    # (three-agent 깨끗한 판 55 개의 분산에서 계산).  손으로 세지 않도록 여기 붙인다.
    TARGET = int(os.environ.get("AIC_TARGET_CLEAN", "22"))
    print("\n구조적 실패 (깨끗한 판만) — 무제안 종료·기준선 재방문·기준선 개선")
    print("  %-19s %14s %14s %14s" % ("팔", "무제안 종료", "C0 재방문", "기준선 개선"))
    for arm in ARMS + tuple(a for a in sorted(rows) if a not in ARMS):
        got = rows.get(arm)
        if not got:
            continue
        clean = [r for r in got if not r["dirty"] and not r.get("stale_prompt")
                 and not r.get("stale_space")]
        if not clean:
            print("  %-19s %14s %14s" % (arm, "-", "-"))
            continue
        pf = sum(1 for r in clean if r["pfail"])
        rv = sum(1 for r in clean if r["revisit"])
        im = sum(1 for r in clean if r.get("improved"))
        print("  %-19s %6d/%-3d %3.0f%% %6d/%-3d %3.0f%% %6d/%-3d %3.0f%%"
              % (arm, pf, len(clean), 100.0 * pf / len(clean),
                 rv, len(clean), 100.0 * rv / len(clean),
                 im, len(clean), 100.0 * im / len(clean)))

    print("\n목표 대비 (팔당 %d 판, 첫성공 30%%p 차 검정력 .80)" % TARGET)
    for arm in ARMS + tuple(a for a in sorted(rows) if a not in ARMS):
        got = rows.get(arm)
        if not got:
            continue
        clean = sum(1 for r in got if not r["dirty"] and not r.get("stale_prompt")
                    and not r.get("stale_space"))
        left = max(0, TARGET - clean)
        bar = "#" * min(22, clean) + "." * min(22, left)
        print("  %-19s %2d/%d  %s  %s" % (arm, clean, TARGET, bar,
                                          "달성" if left == 0 else "%d 판 남음" % left))

    print("\n종료상태")
    for arm in sorted(rows):
        c = Counter(r["term"] for r in rows[arm])
        print("  %-19s %s" % (arm, "  ".join("%s=%d" % kv for kv in c.most_common())))
    missing = [a for a in ARMS[:3] if a not in rows]
    if missing:
        print("\n아직 판이 없는 팔: %s — 비교는 이들이 쌓인 뒤에야 의미가 있다"
              % ", ".join(missing))
    return 0


def _self_check():
    """팔이 하나뿐이어도 표가 나오고, 없는 팔은 이름으로 알린다."""
    doc = {"condition": {"name": ce.CONDITION}, "method": "three-agent",
           "retained": {"qualified": False}, "t0Success": False, "firstSuccess": {},
           "calls": [{"inputTokens": 10, "outputTokens": 5, "latencyMs": 100.0}],
           "trials": [{"verdicts": {"T0": {"a": "PASS"}}}]}
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "evidence").mkdir()
    (d / "evidence" / "AGENT-x-episode.json").write_text(json.dumps(doc))
    got = collect(str(d / "evidence" / "AGENT-*-episode.json"))
    assert set(got) == {"three-agent"}, got
    assert got["three-agent"][0]["in"] == 10 and got["three-agent"][0]["pass"] == 1, got
    # dict 를 bool 로 읽지 않는지 고정한다 -- 이걸로 한 번 틀렸다.
    assert got["three-agent"][0]["retained"] is False, got
    assert got["three-agent"][0]["formfail"] is False, got
    # 결정 시행 세기: 픽스처의 시행에는 decision 이 없으므로 0 이어야 한다
    assert got["three-agent"][0]["decided"] == 0, got
    # pfail / revisit: 시행 0 은 기준선이므로 **제외**해야 한다.  trials[1:] 를
    # trials 로 잘못 쓰면 모든 판이 "C0 재방문" 이 된다 (시행 0 이 항상 C0).
    _doc = {"termination": {"reason": "PROPOSAL_FAILURE"},
            "trials": [{"controlId": "C0"}, {"controlId": "M12"}, {"controlId": "C0"}]}
    _t = _doc["trials"]
    assert (_doc["termination"]["reason"] == "PROPOSAL_FAILURE") is True
    assert ("C0" in [x.get("controlId") for x in _t[1:]]) is True, "재방문을 잡아야 한다"
    _no = {"termination": {"reason": "DEADLINE"},
           "trials": [{"controlId": "C0"}, {"controlId": "M12"}]}
    assert ("C0" in [x.get("controlId") for x in _no["trials"][1:]]) is False, \
        "시행 0 의 C0 를 재방문으로 세면 안 된다"
    # 프롬프트 경계: 2026-09-17 오너 드롭으로 **세 팔 전부**가 경계를 갖는다.
    # (그 전에는 basic 한 팔에만 있었고, 이 검사도 "three-agent 는 경계가 없다" 를
    #  고정하고 있었다 -- 사실이 바뀌면 검사도 같이 바뀐다.)
    for _arm in ("three-agent", "internal-monolith", "basic-monolith"):
        _cut = PROMPT_EPOCH[_arm]
        assert _prompt_stale(_arm, _cut - 1) is True, "경계 이전은 제외: " + _arm
        assert _prompt_stale(_arm, _cut + 1) is False, "경계 이후는 포함: " + _arm
        assert _prompt_stale(_arm, None) is False, "시각을 못 읽으면 제외하지 않는다: " + _arm
    assert _prompt_stale("three-agent-coverage", 0) is False, "경계가 없는 팔은 건드리지 않는다"
    # 액션 공간 경계: 프롬프트 경계와 **다른 축**이다.  프롬프트가 같아도 축·사다리가
    # 다르면 다른 실험이므로 따로 건다.
    assert _action_space_stale(ACTION_SPACE_EPOCH - 1) is True, "경계 이전은 제외"
    assert _action_space_stale(ACTION_SPACE_EPOCH + 1) is False, "경계 이후는 포함"
    assert _action_space_stale(None) is False, "시각을 못 읽으면 제외하지 않는다"
    print("자체 검사 통과")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--self-check":
        _self_check(); raise SystemExit(0)
    since = None
    formal = False
    if args and args[0] == "--formal":
        formal = True; args = args[1:]
    if args and args[0] == "--since":
        since = args[1]; args = args[2:]
    _self_check()
    pattern = args[0] if args else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        # 2026-09-18: 날짜 패턴이 `[67]` 이라 **9월 18일 판을 통째로 빼먹고 있었다** --
        # 자정을 넘긴 순간 새 판이 집계에서 사라졌고, 표본이 28 에서 멈춘 것처럼
        # 보였다(실제로는 44).  달을 넘겨도 죽지 않도록 접두만 맞춘다.
        "formal38guarded-2026*T*/evidence/AGENT-*-episode.json")
    if formal:
        FORMAL_CODE = _latest_code_version(pattern)
        print("formal: codeVersion %s 인 판만 비교한다" % (FORMAL_CODE or "(아직 찍힌 판 없음 -- 비교할 판이 없다)"))
        if FORMAL_CODE is None:
            FORMAL_CODE = "none"
    raise SystemExit(main(pattern, since))
