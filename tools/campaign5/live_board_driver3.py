#!/usr/bin/env python3
"""3-intent / 3-action live board search (real coordination code, real xApp).

Intents (conflicting) — the IntentPriorityCoordinator must decide which to hold:
  I1 (prio 1): UE1 DL >= 3.0 Mbps   (priority user's guaranteed floor)
  I2 (prio 2): UE2 DL >= 1.5 Mbps   (best-effort minimum service)
  I3 (prio 3): UE2 DL <= 1.0 Mbps   (fairness rate-cap)  -- HARD CONFLICT with I2
Actions (conflicting) — the ActionPriorityCoordinator ranks which to defer:
  A1: pfUE1  (Action 103, UE1 PF scheduler weight; boosts UE1)
  A2: pfUE2  (Action 103, UE2 PF scheduler weight; boosts UE2)
  A3: capUE2 (Action 102, UE2 PRB cap; limits UE2)          -- OPPOSES A2 on UE2
The searcher picks each candidate from the observed board; the whole-intent
state comes from the measured DL, and the whole-action state from which axes
moved off baseline. Genuine conflict => the search cannot hold all 3; the best
pair reflects the coordinated priority (hold I1,I2; defer I3).
"""
import json, subprocess, sys, time, datetime, threading
sys.path.insert(0, "/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN")
from assurance.coordination.board import (
    Board, StatePair, PairProvenance, OutcomeKind, ActionVerification,
    intent_states_from_verdicts, action_states_from_candidate)
from assurance.coordination.priorities import (
    IntentPriorityCoordinator, ActionPriorityCoordinator, PairComparator)
from assurance.coordination.search import (
    BoardSearcher, CandidateView, IntentView, ActionAxisView)

SP = "/tmp/claude-1000/-home-ran-node1-agentic-ran-coordinator-based-on-ORAN/3baeea94-ab03-459f-be9c-9424c5c7a596/scratchpad"
EPOCH = 317
UE1 = {"ip": None, "host": "ue1", "amf": None, "ran": 2}   # filled from argv
UE2 = {"ip": None, "host": "ue2", "amf": None, "ran": 3}
# argv: ue1_ip ue1_amf ue2_ip ue2_amf
UE1["ip"], UE1["amf"], UE2["ip"], UE2["amf"] = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
T1, T2c, T3 = 3.0, 1.5, 1.0     # I1 floor, I2 floor, I3 ceiling
HOLD = 7

intents = [IntentView("I1", "ue1"), IntentView("I2", "ue2"), IntentView("I3", "ue2")]
actions = [ActionAxisView("A1", "pfUE1", "ue1", "8", shared=True),
           ActionAxisView("A2", "pfUE2", "ue2", "8", shared=True),
           ActionAxisView("A3", "capUE2", "ue2", "24", shared=True)]
action_axes = {"A1": "pfUE1", "A2": "pfUE2", "A3": "capUE2"}
baselines = {"pfUE1": "8", "pfUE2": "8", "capUE2": "24"}
intent_predicates = {"I1": ["p1"], "I2": ["p2"], "I3": ["p3"]}
catalog = [
    CandidateView("c_base",  {"pfUE1": "8",  "pfUE2": "8",  "capUE2": "24"}),  # baseline
    CandidateView("c_boost1",{"pfUE1": "11", "pfUE2": "5",  "capUE2": "24"}),  # boost UE1 (A1)
    CandidateView("c_boost2",{"pfUE1": "13", "pfUE2": "4",  "capUE2": "24"}),  # stronger UE1
    CandidateView("c_pf2",   {"pfUE1": "8",  "pfUE2": "12", "capUE2": "24"}),  # boost UE2 (A2)
    CandidateView("c_cap",   {"pfUE1": "11", "pfUE2": "5",  "capUE2": "6"}),   # + cap UE2 (A3) vs A2
]
comparator = PairComparator(
    intents=IntentPriorityCoordinator(order=("I1", "I2", "I3")),
    actions=ActionPriorityCoordinator(order=("A1", "A2", "A3")))
searcher = BoardSearcher(catalog=catalog, intents=intents, actions=actions,
                         comparator=comparator, budget_trials=5, undecidable_streak=3)

def sh(c, t=20):
    try: return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t).stdout.strip()
    except Exception: return ""
def rx(ue):
    v = sh(f"ssh -o ConnectTimeout=5 {ue['host']} 'cat /sys/class/net/oaitun_ue1/statistics/rx_bytes 2>/dev/null'", 8)
    try: return int(v)
    except Exception: return None
def fire_pf(ue, w):  return "ACK" in sh(f"{SP}/fire_pf.sh {ue['amf']} {ue['ran']} {w}", 18)
def fire_cap(ue, p): return "ACK" in sh(f"{SP}/fire_cap2.sh {ue['amf']} {ue['ran']} {p}", 18)
def apply_candidate(p):
    fire_pf(UE1, p["pfUE1"]); fire_pf(UE2, p["pfUE2"]); fire_cap(UE2, p["capUE2"])

flood = subprocess.Popen(
    f"for ip in {UE1['ip']} {UE2['ip']}; do docker exec oai-ext-dn timeout 80 bash -c "
    "\"while true; do cat /dev/zero > /dev/tcp/$ip/5201 2>/dev/null; done\" & done; wait", shell=True)
time.sleep(3)

board = Board(); trials = []; trace = []; marks = []; used = 0; t0 = time.time()
now = lambda: round(time.time() - t0, 2)
_stop = threading.Event()
def sampler():
    while not _stop.is_set():
        trace.append({"t": now(), "rx1": rx(UE1), "rx2": rx(UE2)}); time.sleep(1)
threading.Thread(target=sampler, daemon=True).start()

while True:
    d = searcher.next(board, trials_used=used)
    print(f"[searcher] used={used} -> {d.candidate_id} term={d.termination} :: {d.rationale}", flush=True)
    if d.termination is not None:
        termination, final = d.termination.value, d.rationale; break
    cand = next(c for c in catalog if c.candidate_id == d.candidate_id)
    marks.append({"t": now(), "candidateId": cand.candidate_id, "params": dict(cand.parameters), "rationale": d.rationale})
    apply_candidate(cand.parameters)
    r1a, r2a = rx(UE1), rx(UE2); ts = time.time(); time.sleep(HOLD)
    r1b, r2b = rx(UE1), rx(UE2); dt = time.time() - ts
    dl1 = (r1b-r1a)*8/dt/1e6 if (r1a and r1b) else 0.0
    dl2 = (r2b-r2a)*8/dt/1e6 if (r2a and r2b) else 0.0
    verdicts = {"p1": "PASS" if dl1 >= T1 else "FAIL",     # I1 floor
                "p2": "PASS" if dl2 >= T2c else "FAIL",    # I2 floor
                "p3": "PASS" if dl2 <= T3 else "FAIL"}     # I3 ceiling
    istates = intent_states_from_verdicts(intent_predicates, verdicts)
    astates = action_states_from_candidate(cand.parameters, action_axes, baselines)
    pair = StatePair(candidate_id=cand.candidate_id, parameters=cand.parameters,
                     intent_states=istates, action_states=astates,
                     provenance=PairProvenance.OBSERVED, outcome_kind=OutcomeKind.EVALUATED,
                     conditions={"epoch": EPOCH}, trial_id=f"trial:{used+1}",
                     recorded_at=datetime.datetime.utcnow().isoformat()+"Z",
                     detail=f"UE1={dl1:.2f} UE2={dl2:.2f}")
    board.record(pair)
    trials.append({"trialId": pair.trial_id, "candidateId": cand.candidate_id, "params": dict(cand.parameters),
                   "dl1": round(dl1,3), "dl2": round(dl2,3), "verdicts": verdicts, "rationale": d.rationale,
                   "label": pair.label(["I1","I2","I3"], ["A1","A2","A3"])})
    print(f"   {cand.parameters} -> UE1={dl1:.2f} UE2={dl2:.2f} :: {pair.label(['I1','I2','I3'],['A1','A2','A3'])}", flush=True)
    used += 1

_stop.set(); time.sleep(1.2)
try: flood.terminate()
except Exception: pass
best = board.best_observed(comparator)
out = {"schemaVersion": "live-board-search-3intent/1.0.0", "epoch": EPOCH,
       "targets": {"I1_floor": T1, "I2_floor": T2c, "I3_ceiling": T3},
       "comparator": comparator.to_record(),
       "catalog": [{"candidateId": c.candidate_id, "parameters": dict(c.parameters)} for c in catalog],
       "trials": trials, "termination": termination, "finalRationale": final,
       "bestPair": best.to_record() if best else None,
       "bestLabel": best.label(["I1","I2","I3"], ["A1","A2","A3"]) if best else None,
       "searcherDecisions": [x.to_record() for x in searcher.decisions],
       "trace": trace, "marks": marks}
json.dump(out, open(f"{SP}/live_board3_run.json", "w"), indent=2)
print(f"\nTERMINATION: {termination}")
if best: print(f"BEST COORDINATED PAIR: {out['bestLabel']}  ({best.detail})")
print(f"trials={len(trials)} trace={len(trace)} -> live_board3_run.json")
