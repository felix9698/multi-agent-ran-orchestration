"""Batch G (P0-18): scheduler KPI code-path efficacy qualification.

On a shared-cell EMULATED path (any cell serving >= 2 UEs; the current testbed's
shared cell is UE1+UE2 on gNB1 - the scope is derived dynamically from the
topology, never a fixed 3-UE assumption), drive ONE real sched_priority
change through the REAL scheduler action executor (set_sched_priority) + verified
readback (get_sched_priority) - NEVER a direct channel mutation disguised as an
action - and compare a controlled NEUTRAL baseline with the post-action per-UE
throughput measured through the same collector. Records actual per-UE
throughput before/after/delta and an HONEST verdict:

  VERIFIED - readback == requested, every scoped per-UE KPI is a finite non-bool
             sample, and the boosted UE's KPI rose past the effect threshold
             while its cell-mates were redistributed downward.
  FAILED   - action not applied / readback mismatch / no measurable effect /
             wrong direction (never counted as success).
  UNKNOWN  - a missing / invalid / non-finite / bool KPI sample, or the target is
             not a shared/contending cell (cannot qualify).

Output is ALWAYS integration_only / excluded_from_paper / not paper_ready and
hardware_unverified - it is a code-path efficacy check on the emulated channel,
never a paper/OTA performance claim.
"""
import math
from typing import Dict, List, Optional

_NEUTRAL = 1.0                        # PF-neutral weight (matches executor.PRIO_NEUTRAL)


def _valid_kpi(x) -> bool:
    """A finite, non-bool real KPI sample (bool / None / NaN / inf rejected)."""
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


def _pos_real(x) -> bool:
    """A finite, non-bool real number > 0 (bool / NaN / inf / <= 0 rejected)."""
    return _valid_kpi(x) and x > 0


def _measure(collector, scope: List[str]) -> Dict[str, Optional[float]]:
    """Per-scoped-UE throughput from the SAME collector used everywhere else. A
    UE absent from the probe is honest None (UNKNOWN), never a fabricated 0."""
    probe = collector.get_throughput_all()
    return {ue: probe.get(ue) for ue in scope}


def _readback_matches(readback, requested) -> bool:
    return (_valid_kpi(readback) and _valid_kpi(requested)
            and abs(float(readback) - float(requested)) < 1e-9)


def qualify_scheduler_efficacy(coordinator, boost_ue: str, weight: float,
                               effect_threshold_mbps: float = 0.05,
                               data_origin: str = "emulated_pipeline") -> Dict:
    """Qualify sched_priority efficacy for ONE boosted UE on its shared cell."""
    executor = coordinator.executor
    collector = coordinator.ue_collector
    ue_serving = dict(getattr(coordinator, "ue_serving_gnb", {}))
    ue_rnti = dict(getattr(coordinator, "ue_rnti", {}))

    result = {
        "data_origin": data_origin,
        "paper_eligibility": "integration_only",
        "excluded_from_paper": True, "paper_ready": False,
        "hardware_unverified": True,          # emulated code-path only, no OTA
        "kpi_unit": "Mbps",
        "effect_threshold_mbps": effect_threshold_mbps,
        "action": {"axis": "sched_priority", "target_ue": boost_ue,
                   "requested_weight": weight, "neutral_weight": _NEUTRAL},
    }

    # FAIL CLOSED on a malformed weight / effect threshold BEFORE any action: a
    # bool / non-finite / non-positive lever is not a qualifiable request.
    if not _pos_real(weight) or not _pos_real(effect_threshold_mbps):
        result.update({"verdict": "UNKNOWN", "status": "invalid_input",
                       "reason": "weight and effect_threshold_mbps must be finite "
                                 "non-bool numbers > 0 - refusing to act"})
        return result

    cell = ue_serving.get(boost_ue)
    if cell is None:
        result.update({"verdict": "UNKNOWN", "status": "scope_unknown",
                       "reason": "boost UE has no serving cell"})
        return result
    # DYNAMIC shared-cell scope from the topology attachment (never hardcoded):
    # every UE currently served by the boosted UE's cell is a contender.
    scope = sorted(u for u, g in ue_serving.items() if g == cell)
    result["cell"] = cell
    result["scoped_ues"] = scope
    if len(scope) < 2 or boost_ue not in scope:
        result.update({"verdict": "UNKNOWN", "status": "not_shared_cell",
                       "reason": "target cell has < 2 contending UEs "
                                 "(no scheduler redistribution to qualify)"})
        return result

    # Holding the ENVIRONMENT identical across baseline and post is MANDATORY: the
    # channel must expose capture/restore, else the A/B cannot isolate the ACTION
    # effect and the result is UNKNOWN (never VERIFIED against channel noise).
    channel = getattr(collector, "channel", None)
    can_hold = (channel is not None
                and callable(getattr(channel, "capture_state", None))
                and callable(getattr(channel, "restore_state", None)))
    if not can_hold:
        result.update({"verdict": "UNKNOWN", "status": "environment_not_held",
                       "reason": "channel cannot capture/restore an identical "
                                 "environment - refusing to qualify against noise"})
        return result

    rntis = {u: ue_rnti.get(u) for u in scope}
    entry: Dict[str, object] = {}
    result["scheduler_restore_verified"] = None
    try:
        # capture the ENTRY scheduler state for EVERY scoped UE so it can be restored
        entry = {u: executor.get_sched_priority(cell, rnti=rntis[u]) for u in scope}
        result["entry_weights"] = dict(entry)
        # (baseline) ESTABLISH + readback-verify a NEUTRAL weight for EVERY scoped
        # contending UE (never assume the entry state IS neutral). A failure to set
        # or read back neutral for any scoped UE is UNKNOWN (no honest baseline).
        for u in scope:
            executor.set_sched_priority(cell, _NEUTRAL, rnti=rntis[u])
        neutral_rb = {u: executor.get_sched_priority(cell, rnti=rntis[u])
                      for u in scope}
        result["neutral_baseline"] = neutral_rb
        result["neutral_baseline_verified"] = all(
            _readback_matches(neutral_rb[u], _NEUTRAL) for u in scope)
        if not result["neutral_baseline_verified"]:
            result.update({"verdict": "UNKNOWN", "status": "baseline_not_established",
                           "reason": "could not set + readback a neutral baseline "
                                     "for every scoped contending UE"})
            return result

        # Controlled A/B: capture the channel state, measure the neutral baseline,
        # RESTORE the identical environment, apply ONE real scheduler action, and
        # re-measure - so the delta is the PURE action effect (not draw noise). The
        # channel is NEVER mutated as a disguised action.
        snap = channel.capture_state()
        before = _measure(collector, scope)
        channel.restore_state(snap)                        # identical environment
        applied_ok = bool(executor.set_sched_priority(cell, weight,
                                                      rnti=rntis[boost_ue]))
        readback = executor.get_sched_priority(cell, rnti=rntis[boost_ue])
        after = _measure(collector, scope)

        result["environment_held_constant"] = True
        result["request"] = {"cell": cell, "rnti": rntis[boost_ue],
                             "weight": weight}
        result["applied"] = {"ok": applied_ok}
        result["readback"] = {"weight": readback,
                              "matches_request": _readback_matches(readback, weight)}
        result["before_mbps"] = before
        result["after_mbps"] = after

        # KPI validity: every scoped before/after sample must be a real finite value
        if not (all(_valid_kpi(v) for v in before.values())
                and all(_valid_kpi(v) for v in after.values())):
            result.update({"verdict": "UNKNOWN", "status": "invalid_kpi",
                           "reason": "missing / non-finite / bool per-UE KPI sample"})
            return result

        deltas = {ue: after[ue] - before[ue] for ue in scope}
        result["delta_mbps"] = deltas

        # verified readback is REQUIRED - an unapplied action / readback mismatch
        # is a FAILED qualification, never success.
        if not applied_ok or not _readback_matches(readback, weight):
            result.update({"verdict": "FAILED", "status": "readback_mismatch",
                           "reason": "action not applied or readback != requested"})
            return result

        boost_delta = deltas[boost_ue]
        redistribution_ok = all(deltas[u] <= 1e-9 for u in scope if u != boost_ue)
        result["redistribution_ok"] = bool(redistribution_ok)
        if boost_delta < 0:
            result.update({"verdict": "FAILED", "status": "wrong_direction",
                           "reason": f"boosted UE KPI fell ({boost_delta:.3f} Mbps)"})
            return result
        if boost_delta < effect_threshold_mbps:
            result.update({"verdict": "FAILED", "status": "no_effect",
                           "reason": f"boosted UE delta {boost_delta:.3f} Mbps "
                                     f"below the effect threshold"})
            return result
        # VERIFIED REQUIRES a genuine redistribution: the boosted UE rose AND every
        # contending UE was pushed down. A rise without redistribution is FAILED.
        if not redistribution_ok:
            result.update({"verdict": "FAILED", "status": "wrong_redistribution",
                           "reason": "boosted UE rose but a contending UE did not "
                                     "redistribute down"})
            return result
        result.update({
            "verdict": "VERIFIED", "status": "ok",
            "reason": "readback-verified sched_priority raised the boosted UE past "
                      "the effect threshold with contending UEs redistributed down"})
        return result
    except Exception as exc:
        # ANY capture / restore / neutral / readback / probe error is a finite
        # UNKNOWN with an explicit status - it NEVER escapes the qualification.
        result.update({"verdict": "UNKNOWN", "status": "qualification_exception",
                       "reason": f"scheduler qualification raised: {exc!r}"})
        return result
    finally:
        # ALWAYS restore the ENTRY scheduler state for every CAPTURED scoped UE
        # (never leave the run in the qualification's neutral/boosted state) +
        # readback-verify. If entry capture never happened, there is nothing to
        # restore and it cannot be verified.
        try:
            for u in entry:
                executor.set_sched_priority(cell, entry[u], rnti=rntis[u])
            restored = {u: executor.get_sched_priority(cell, rnti=rntis[u])
                        for u in entry}
            result["scheduler_restore_verified"] = bool(entry) and all(
                _readback_matches(restored[u], entry[u]) for u in entry)
        except Exception:
            result["scheduler_restore_verified"] = False
        # if the entry state was NOT restored + readback-verified, a prior VERIFIED
        # can no longer stand: downgrade it to FAILED restore_unverified (a run left
        # in an unverified scheduler state is never a success).
        if (result.get("scheduler_restore_verified") is not True
                and result.get("verdict") == "VERIFIED"):
            result.update({
                "verdict": "FAILED", "status": "restore_unverified",
                "reason": "entry scheduler state was not restored + "
                          "readback-verified after the qualification"})
