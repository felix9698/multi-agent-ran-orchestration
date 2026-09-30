#!/usr/bin/env python3
"""Live board-search driver: the REAL coordination code drives real xApp actions.

BoardSearcher (Role 3) picks the next candidate from the observed board under
the two priority coordinators (Roles 1 & 2). Each candidate is applied on the
radio via our xApp (E2SM-RC Action 102 PRB cap), the per-UE downlink is measured
from the UE tun byte counters, verdicts against the intents' throughput targets
become the whole-intent state, and the pair is recorded back on the board. The
searcher then chooses again, until FOUND / BUDGET_EXHAUSTED / CATALOG_EXHAUSTED.

Nothing here is a fixed script: the candidate ORDER is the searcher's decision.
"""
import json, subprocess, sys, time, datetime
sys.path.insert(0, "/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN")

from assurance.coordination.board import (
    Board, StatePair, PairProvenance, OutcomeKind, ActionVerification,
    intent_states_from_verdicts, action_states_from_candidate)
from assurance.coordination.priorities import (
    IntentPriorityCoordinator, ActionPriorityCoordinator, PairComparator)
from assurance.coordination.search import (
    BoardSearcher, CandidateView, IntentView, ActionAxisView, TerminationReason)

SP = "/tmp/claude-1000/-home-ran-node1-agentic-ran-coordinator-based-on-ORAN/3baeea94-ab03-459f-be9c-9424c5c7a596/scratchpad"
EPOCH = 317
# amf<->UE mapping confirmed by cap-and-observe (capping an amf lowers THAT UE):
UE1 = {"ip": "12.1.1.141", "host": "ue1", "amf": 44, "ran": 2}
UE2 = {"ip": "12.1.1.13",  "host": "ue2", "amf": 43, "ran": 1}
T1, T2 = 2.3, 1.8          # per-intent DL throughput targets (Mbps)
HOLD = 8                    # seconds measured per trial
BASE_PRB = "24"

# ---- the two priority roles, the catalog, the searcher -----------------------
intents = [IntentView("I1", scope_id="ue1"), IntentView("I2", scope_id="ue2")]
actions = [
    ActionAxisView("A1", axis="capUE1", scope_id="ue1", baseline=BASE_PRB, shared=True),
    ActionAxisView("A2", axis="capUE2", scope_id="ue2", baseline=BASE_PRB, shared=True),
]
action_axes = {"A1": "capUE1", "A2": "capUE2"}
baselines = {"capUE1": BASE_PRB, "capUE2": BASE_PRB}
intent_predicates = {"I1": ["p_I1_dl"], "I2": ["p_I2_dl"]}
catalog = [  # mild caps only (>=12 PRB) so a hard cap never crashes a weak UE
    CandidateView("k_2424", {"capUE1": "24", "capUE2": "24"}),
    CandidateView("k_2412", {"capUE1": "24", "capUE2": "12"}),
    CandidateView("k_1224", {"capUE1": "12", "capUE2": "24"}),
    CandidateView("k_1212", {"capUE1": "12", "capUE2": "12"}),
    CandidateView("k_2418", {"capUE1": "24", "capUE2": "18"}),
    CandidateView("k_1818", {"capUE1": "18", "capUE2": "18"}),
]
comparator = PairComparator(
    intents=IntentPriorityCoordinator(order=("I1", "I2")),   # I1 protected first
    actions=ActionPriorityCoordinator(order=("A1", "A2")))
searcher = BoardSearcher(catalog=catalog, intents=intents, actions=actions,
                         comparator=comparator, budget_trials=6, undecidable_streak=2)

# ---- radio I/O ---------------------------------------------------------------
def sh(cmd, timeout=20):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""

def rx(ue):
    v = sh(f"ssh -o ConnectTimeout=5 {ue['host']} "
           f"'cat /sys/class/net/oaitun_ue1/statistics/rx_bytes 2>/dev/null'", 8)
    try: return int(v)
    except Exception: return None

def fire_cap(ue, prbs):
    out = sh(f"{SP}/fire_cap2.sh {ue['amf']} {ue['ran']} {prbs}", 20)
    return "ACK" in out

def apply_candidate(params):
    ok1 = fire_cap(UE1, params["capUE1"]); ok2 = fire_cap(UE2, params["capUE2"])
    return ok1, ok2

# continuous DL flood to both UEs for the whole run
flood = subprocess.Popen(
    f"for ip in {UE1['ip']} {UE2['ip']}; do "
    "docker exec oai-ext-dn timeout 90 bash -c "
    "\"while true; do cat /dev/zero > /dev/tcp/$ip/5201 2>/dev/null; done\" & done; wait",
    shell=True)
time.sleep(3)

# ---- the search loop ---------------------------------------------------------
board = Board(); trials = []; trace = []; marks = []; trials_used = 0
t0 = time.time()
def now_rel(): return round(time.time() - t0, 2)

# background trace sampler thread (per-second both-UE rx while trials run)
import threading
_stop = threading.Event()
def sampler():
    prev = {UE1["ip"]: None, UE2["ip"]: None}
    while not _stop.is_set():
        r1, r2 = rx(UE1), rx(UE2)
        trace.append({"t": now_rel(), "rx1": r1, "rx2": r2})
        time.sleep(1)
threading.Thread(target=sampler, daemon=True).start()

while True:
    decision = searcher.next(board, trials_used=trials_used)
    print(f"[searcher] trials_used={trials_used} -> candidate={decision.candidate_id} "
          f"term={decision.termination} :: {decision.rationale}", flush=True)
    if decision.termination is not None:
        termination = decision.termination.value
        final_rationale = decision.rationale
        break
    cand = next(c for c in catalog if c.candidate_id == decision.candidate_id)
    marks.append({"t": now_rel(), "candidateId": cand.candidate_id,
                  "params": dict(cand.parameters), "rationale": decision.rationale})
    ok1, ok2 = apply_candidate(cand.parameters)
    r1a, r2a = rx(UE1), rx(UE2); ts = time.time()
    time.sleep(HOLD)
    r1b, r2b = rx(UE1), rx(UE2); dt = time.time() - ts
    dl1 = (r1b - r1a) * 8 / dt / 1e6 if (r1a and r1b) else 0.0
    dl2 = (r2b - r2a) * 8 / dt / 1e6 if (r2a and r2b) else 0.0
    if dl1 + dl2 < 0.15:   # both ~0: transient flood/sink drop -> re-ensure sinks, re-measure once
        for u in (UE1, UE2):
            sh(f"ssh -o ConnectTimeout=5 {u['host']} 'ss -ltn 2>/dev/null | grep -q \":5201 \" || "
               f"(fuser -k 5201/tcp 2>/dev/null; nohup setsid python3 /tmp/srv.py >/tmp/srv.log 2>&1 </dev/null & disown)'", 8)
        r1a, r2a = rx(UE1), rx(UE2); ts = time.time(); time.sleep(HOLD)
        r1b, r2b = rx(UE1), rx(UE2); dt = time.time() - ts
        dl1 = (r1b - r1a) * 8 / dt / 1e6 if (r1a and r1b) else 0.0
        dl2 = (r2b - r2a) * 8 / dt / 1e6 if (r2a and r2b) else 0.0
    verdicts = {"p_I1_dl": "PASS" if dl1 >= T1 else "FAIL",
                "p_I2_dl": "PASS" if dl2 >= T2 else "FAIL"}
    intent_states = intent_states_from_verdicts(intent_predicates, verdicts)
    action_states = action_states_from_candidate(cand.parameters, action_axes, baselines)
    av = {aid: (ActionVerification.NOT_EXECUTED if action_states[aid].value == "DEFERRED"
                else (ActionVerification.VERIFIED if (ok1 if aid == "A1" else ok2)
                      else ActionVerification.UNVERIFIED))
          for aid in action_axes}
    pair = StatePair(candidate_id=cand.candidate_id, parameters=cand.parameters,
                     intent_states=intent_states, action_states=action_states,
                     provenance=PairProvenance.OBSERVED, outcome_kind=OutcomeKind.EVALUATED,
                     conditions={"epoch": EPOCH, "ue1_amf": UE1["amf"], "ue2_amf": UE2["amf"]},
                     trial_id=f"trial:{trials_used+1}",
                     recorded_at=datetime.datetime.utcnow().isoformat()+"Z",
                     detail=f"DL UE1={dl1:.2f} UE2={dl2:.2f} Mbps (T1={T1} T2={T2})",
                     action_verification=av)
    board.record(pair)
    trials.append({"trialId": pair.trial_id, "candidateId": cand.candidate_id,
                   "params": dict(cand.parameters), "dl1": round(dl1, 3), "dl2": round(dl2, 3),
                   "verdicts": verdicts, "rationale": decision.rationale,
                   "pair": pair.to_record(),
                   "label": pair.label(["I1", "I2"], ["A1", "A2"])})
    print(f"   applied {cand.parameters} -> UE1={dl1:.2f} UE2={dl2:.2f} "
          f":: {pair.label(['I1','I2'],['A1','A2'])}", flush=True)
    trials_used += 1

_stop.set(); time.sleep(1.2)
try: flood.terminate()
except Exception: pass

out = {"schemaVersion": "live-board-search/1.0.0", "epoch": EPOCH,
       "targets": {"I1": T1, "I2": T2}, "ue1": UE1, "ue2": UE2,
       "comparator": comparator.to_record(),
       "catalog": [{"candidateId": c.candidate_id, "parameters": dict(c.parameters)} for c in catalog],
       "trials": trials, "termination": termination, "finalRationale": final_rationale,
       "searcherDecisions": [d.to_record() for d in searcher.decisions],
       "trace": trace, "marks": marks}
json.dump(out, open(f"{SP}/live_board_run.json", "w"), indent=2)
print(f"\nTERMINATION: {termination}\n{final_rationale}")
print(f"trials={len(trials)} trace_samples={len(trace)} -> {SP}/live_board_run.json")
