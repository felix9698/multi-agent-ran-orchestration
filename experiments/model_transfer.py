"""Batch G item 5: a SHORT model-transfer (hot-swap) phase comparison.

ONE ExperimentRunner / IntentCoordinator / topology throughout; ONLY the
proposer/model provenance changes. This module REUSES (never rewrites/weakens)
IntentCoordinator.set_llm_backend (the queued hot-swap) and get_switch_audit (the
proposal-boundary audit): it CONSUMES the finalized switch audit + the finalized
EpisodeRecord evidence and attributes a pre/post split by EVIDENCE
(proposer/model), NEVER by the request point. When no applied switch audit AND a
matching EpisodeRecord evidence exist, it returns an honest not_applied/unknown
result and never fabricates pre/post values.

Synthetic/emulated output is ALWAYS integration_only / excluded_from_paper /
paper_ready=False - it can never claim paper performance.
"""
from collections import defaultdict
from typing import Dict, List, Optional

from experiments.metrics import (
    StepRecord, EpisodeRecord, IntentConfig, strict_joint_satisfaction,
    _nonneg_real,
)

# coordinator.proposer.SWITCH_APPLIED value ("applied"); duplicated as a literal
# so the experiments layer does not import the coordinator. A test asserts this
# literal still equals coordinator.proposer.SWITCH_APPLIED (fail-closed on drift).
SWITCH_APPLIED_STATUS = "applied"

# a proposal DECISION is ACCEPTED iff its finalized terminal is a commit.
_COMMIT_PREFIX = "commit"


def _episode_model(ep) -> Optional[str]:
    """The finalized-evidence model identity of an episode: proposer_id preferred,
    else model_version. None when the evidence recorded neither (never guessed)."""
    pid = getattr(ep, "evidence_proposer_id", None)
    mv = getattr(ep, "evidence_model_version", None)
    return pid or mv or None


def find_applied_switch(switch_audit: List[Dict], target_model: str,
                        request_id: Optional[str] = None):
    """Resolve THE applied switch to target_model, returning (entry, status):
      * ("applied") - a single unambiguous applied switch (status==applied AND a
        real applied_at). When `request_id` is given it must resolve EXACTLY one
        entry; otherwise there must be exactly one applied switch to the model.
      * (None, "not_applied") - no such applied switch.
      * (None, "ambiguous") - MULTIPLE applied switches target the model and no
        (or a non-unique) request_id was given - the first historical one is
        NEVER silently chosen."""
    matches = [dict(e) for e in (switch_audit or [])
               if e.get("status") == SWITCH_APPLIED_STATUS
               and e.get("target") == target_model and e.get("applied_at")]
    if request_id is not None:
        by_rid = [m for m in matches if m.get("request_id") == request_id]
        if len(by_rid) == 1:
            return by_rid[0], "applied"
        return None, ("not_applied" if not by_rid else "ambiguous")
    if len(matches) == 1:
        return matches[0], "applied"
    return (None, "not_applied") if not matches else (None, "ambiguous")


def identify_first_effective(episodes: List[EpisodeRecord], target_model: str):
    """(index, episode) of the FIRST EpisodeRecord whose finalized evidence model
    == target_model - by EVIDENCE, not the request point. (None, None) if none."""
    for i, ep in enumerate(episodes):
        if _episode_model(ep) == target_model:
            return i, ep
    return None, None


def rebase_steps(steps: List[StepRecord]) -> List[StepRecord]:
    """Rebase step_idx to 0-based CONTIGUOUS within each (method, trial_id) window
    (sorted by the original step_idx) so strict_joint_satisfaction's contiguity /
    min==0 holds AFTER a mid-run pre/post split. ONLY step_idx is reassigned;
    every BOUNDARY IDENTIFIER (trial_id, method, phase, phase_idx, phase_label)
    and the payload (t_s, ue_kpis, action_offsets) is PRESERVED - the window
    IDENTITY is never corrupted."""
    groups: Dict = defaultdict(list)
    for s in steps:
        groups[(s.method, s.trial_id)].append(s)
    out: List[StepRecord] = []
    for key in groups:
        window = groups[key]
        idxs = [s.step_idx for s in window]
        # only a VALID unique CONTIGUOUS non-negative PLAIN-int slice may be
        # rebased. bool / non-int / negative / duplicate / gap is MALFORMED and
        # FAILS CLOSED - it is NEVER normalized into a valid contiguous window.
        if (not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                    for v in idxs)
                or len(idxs) != len(set(idxs))
                or (max(idxs) - min(idxs) + 1) != len(idxs)):
            raise ValueError(
                f"rebase_steps: window {key} has malformed step_idx {idxs} "
                f"(bool/non-int/negative/duplicate/gap) - refusing to normalize "
                f"invalid evidence into a valid contiguous window")
        for new_idx, s in enumerate(
                sorted(window, key=lambda x: x.step_idx)):
            out.append(StepRecord(
                trial_id=s.trial_id, method=s.method, phase=s.phase,
                step_idx=new_idx, t_s=s.t_s,
                ue_kpis=s.ue_kpis, action_offsets=s.action_offsets,
                phase_idx=s.phase_idx, phase_label=s.phase_label))
    return out


def budget_consumption(episodes: List[EpisodeRecord]) -> Dict:
    """EXACT per-cycle budget consumption from EpisodeRecord.budget_* - a measured
    debit when a real non-negative debit is present, else its lower bound with an
    honest unknown status. bool/non-finite/negative debit values are UNKNOWN
    (never a fabricated zero); a measured 0.0 is valid. Units are 'budget' (the
    coordinator's own cost units)."""
    debited = 0.0
    lower_bound = 0.0
    n_measured = n_lower_bound = n_unknown = n_none = 0
    statuses: Dict = {}
    # a debit is a COMPLETE measured settlement only when its status says so; the
    # ledger already sets budget_debited non-None ONLY when every accepted entry
    # settled, but we ALSO honor budget_debit_status explicitly (a debit paired
    # with a non-settled status is downgraded to a lower bound - never a fake
    # complete debit).
    _complete = (None, "", "settled", "complete")
    for e in episodes:
        d = getattr(e, "budget_debited", None)
        lb = getattr(e, "budget_debited_lower_bound", None)
        st = getattr(e, "budget_debit_status", None)
        statuses[st] = statuses.get(st, 0) + 1
        if _nonneg_real(d) and st in _complete:
            debited += float(d)
            lower_bound += float(d)
            n_measured += 1
        elif _nonneg_real(d):
            lower_bound += float(d)           # debit present but status incomplete
            n_lower_bound += 1
        elif _nonneg_real(lb):
            lower_bound += float(lb)          # partial/unknown debit -> LB only
            n_lower_bound += 1
        elif d is None and lb is None:
            n_none += 1                       # no ledger for this cycle
        else:
            n_unknown += 1                    # bool/non-finite/negative -> unknown
    has_lb_unknown = (n_lower_bound + n_unknown) > 0
    # MISSING ledger coverage (n_none) ALSO makes a mixed aggregate partial /
    # lower-bound: a measured+none mix is NEVER "measured-complete".
    incomplete = has_lb_unknown or n_none > 0
    if n_measured == 0:
        status = ("unknown_only" if has_lb_unknown else "no_samples")
    elif incomplete:
        status = "partial"
    else:
        status = "measured"
    return {
        "debited": debited, "lower_bound": lower_bound, "unit": "budget",
        "is_lower_bound": incomplete,
        "n_measured": n_measured, "n_lower_bound": n_lower_bound,
        "n_unknown": n_unknown, "n_none": n_none, "status": status,
        # exact per-cycle debit-status coverage (JSON-safe: None key -> "none")
        "debit_status_coverage": {(k or "none"): v for k, v in statuses.items()},
    }


def proposal_acceptance(episodes: List[EpisodeRecord]) -> Dict:
    """Proposal-acceptance fraction from EXACT terminal evidence: a commit* terminal
    is ACCEPTED; any other (reject / negotiation_only / failsafe) is not. A record
    with no explicit terminal_outcome string is UNKNOWN (excluded, counted), never
    guessed."""
    accepted = n_decisions = n_unknown = 0
    for e in episodes:
        t = getattr(e, "terminal_outcome", None)
        if not isinstance(t, str) or not t:
            n_unknown += 1
            continue
        n_decisions += 1
        if t.startswith(_COMMIT_PREFIX):
            accepted += 1
    return {
        "rate": (accepted / n_decisions) if n_decisions else None,
        "n_accepted": accepted, "n_decisions": n_decisions,
        "n_unknown": n_unknown,
        "status": ("measured" if n_decisions else "no_samples"),
        "definition": "commit* terminal == accepted (exact terminal_outcome)",
    }


def _side_metrics(eps: List[EpisodeRecord], sts: List[StepRecord],
                  cfg: IntentConfig) -> Dict:
    """The SIX required outcomes for one side (pre or post). Metrics 1-4 reuse the
    P1-2 strict_joint_satisfaction (its violation_area keeps throughput Mbps*step
    and power dB*step separate); 5-6 are budget + acceptance. Steps are rebased so
    the strict windows are contiguous."""
    sj = strict_joint_satisfaction(rebase_steps(sts), eps, cfg)
    return {
        "n_episodes": len(eps),
        "strict_joint_satisfaction": {           # (1)
            "rate": sj["strict_joint_rate"], "status": sj["rate_status"],
            "n_windows": sj["n_windows"],
            "n_verifiable_windows": sj["n_verifiable_windows"], "ci95": sj["ci95"]},
        "rollback_verified": sj["rollback_verified"],   # (2)
        "violation_area": sj["violation_area"],         # (3) throughput/power sep.
        "recovery": sj["recovery"],                     # (4) time (s)
        "budget": budget_consumption(eps),              # (5)
        "acceptance": proposal_acceptance(eps),         # (6)
    }


def model_transfer_comparison(episodes: List[EpisodeRecord],
                              steps: List[StepRecord], cfg: IntentConfig,
                              switch_audit: List[Dict], target_model: str,
                              from_model: Optional[str] = None,
                              request_id: Optional[str] = None,
                              data_origin: str = "emulated_pipeline",
                              topology_fingerprint: Optional[str] = None) -> Dict:
    """Pre/post six-metric comparison across ONE evidence-attributed model switch.

    Splits `episodes`/`steps` at the FIRST EpisodeRecord whose finalized evidence
    model == target_model (never the request point). Requires BOTH an applied
    switch audit for target_model AND that evidence match; otherwise returns an
    honest not_applied/unknown result with pre/post=None (no fabrication).

    ALWAYS integration_only / excluded_from_paper / paper_ready=False."""
    result = {
        "data_origin": data_origin,
        "topology_fingerprint": topology_fingerprint,   # one topology throughout
        "paper_eligibility": "integration_only",
        "excluded_from_paper": True, "paper_ready": False,
        "target_model": target_model, "from_model": from_model,
    }
    applied, switch_status = find_applied_switch(switch_audit, target_model,
                                                 request_id)
    idx, eff_ep = identify_first_effective(episodes, target_model)
    if switch_status == "ambiguous":
        result.update({
            "applied": False, "status": "ambiguous_switch",
            "reason": ("multiple applied switches target this model and no "
                       "unique request_id was given - refusing to pick one"),
            "switch": None, "first_effective": None, "pre": None, "post": None})
        return result
    if applied is None or idx is None:
        result.update({
            "applied": False, "status": "not_applied",
            "reason": ("no applied switch audit for target model"
                       if applied is None
                       else "no EpisodeRecord evidence matches the target model"),
            "switch": applied, "first_effective": None,
            "pre": None, "post": None})
        return result

    # boundary FROM EVIDENCE: the effective trial (the switch was requested at a
    # trial/proposal boundary, so trial_id cleanly separates pre from post - no
    # cycle/model mixing within a window).
    eff_trial = eff_ep.trial_id
    pre_eps = [e for e in episodes if e.trial_id < eff_trial]
    post_eps = [e for e in episodes if e.trial_id >= eff_trial]
    pre_models = {_episode_model(e) for e in pre_eps if _episode_model(e)}
    post_models = {_episode_model(e) for e in post_eps if _episode_model(e)}
    inferred_from = (next(iter(pre_models)) if len(pre_models) == 1 else None)

    # EXACT audit<->evidence binding: the applied audit's episode/cycle/proposal
    # ids must match the first-effective EpisodeRecord's evidence ids (only where
    # the audit actually carries them). A disagreement is an integrity fault.
    def _bound(audit_key, ev_attr):
        av = applied.get(audit_key)
        ev = getattr(eff_ep, ev_attr, None)
        # if the audit CARRIES the id, the evidence MUST carry an EQUAL one; a
        # MISSING evidence id while the audit has one is UNBOUND, not success.
        return av is None or (ev is not None and av == ev)
    audit_evidence_bound = (_bound("episode_id", "evidence_episode_id")
                            and _bound("cycle_id", "evidence_cycle_id")
                            and _bound("proposal_id", "evidence_proposal_id"))

    switch = {k: applied.get(k) for k in (
        "request_id", "requested_at", "target", "applied_at", "episode_id",
        "cycle_index", "cycle_id", "proposal_id", "first_effective_cycle")}
    first_effective = {
        "trial_id": eff_trial,
        "evidence_proposer_id": getattr(eff_ep, "evidence_proposer_id", None),
        "evidence_model_version": getattr(eff_ep, "evidence_model_version", None),
        "evidence_episode_id": getattr(eff_ep, "evidence_episode_id", None),
        "evidence_cycle_id": getattr(eff_ep, "evidence_cycle_id", None),
        "evidence_proposal_id": getattr(eff_ep, "evidence_proposal_id", None)}
    # cross-model mixing = the target appears pre-switch OR a non-target model
    # appears post-switch. On mixing (or an unbound audit) the pre/post split
    # cannot be cleanly attributed to ONE model per side, so NO metrics are
    # emitted (never a silently mixed-model comparison).
    cross_model_mixing = (target_model in pre_models
                          or bool(post_models - {target_model}))
    result.update({
        "applied": True, "from_model": from_model or inferred_from,
        "switch": switch, "first_effective": first_effective,
        "cross_model_mixing": cross_model_mixing,
        "audit_evidence_bound": audit_evidence_bound})
    if cross_model_mixing or not audit_evidence_bound:
        result.update({
            "status": ("cross_model_mixing" if cross_model_mixing
                       else "audit_evidence_unbound"),
            "reason": ("cycles/models are mixed across the pre/post boundary"
                       if cross_model_mixing else
                       "applied audit ids do not match the first-effective "
                       "evidence ids"),
            "pre": None, "post": None})
        return result
    result.update({
        "status": "applied",
        "pre": _side_metrics(pre_eps, [s for s in steps
                                       if s.trial_id < eff_trial], cfg),
        "post": _side_metrics(post_eps, [s for s in steps
                                         if s.trial_id >= eff_trial], cfg),
    })
    return result


def request_transfer_switch(coordinator, to_model) -> Dict:
    """Request EXACTLY ONE model switch during an active run, at the coordinator's
    proposal boundary. REUSES IntentCoordinator.set_llm_backend (the queued
    hot-swap) - never a private bypass - and returns the audited request binding
    (request_id / requested_at / target / status) from get_switch_audit for the
    NEWEST entry targeting to_model. The applied episode/cycle/proposal ids are
    filled by the coordinator at the boundary; read them later via
    get_switch_audit(). Returns {'accepted': False} if set_llm_backend refused."""
    target = str(getattr(to_model, "value", to_model))
    # FAIL CLOSED unless an episode is ACTIVELY in flight: a model transfer is a
    # QUEUED hot-swap applied at the next proposal boundary of a live run - an
    # idle switch is not a transfer and would apply immediately, so refuse it.
    if not getattr(coordinator, "_episode_in_flight", False):
        return {"accepted": False, "status": "no_active_run",
                "request_id": None, "requested_at": None, "target": target}
    accepted = bool(coordinator.set_llm_backend(to_model))
    entry = None
    for e in reversed(coordinator.get_switch_audit()):
        if e.get("target") == target:
            entry = e
            break
    return {
        "accepted": accepted,
        "request_id": (entry or {}).get("request_id"),
        "requested_at": (entry or {}).get("requested_at"),
        "target": target,
        "status": (entry or {}).get("status"),
    }
