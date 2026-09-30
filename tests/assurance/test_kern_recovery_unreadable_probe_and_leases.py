"""Kernel recovery defects 2 and 4 of the 2026-09-23 audit.  Hermetic.

2: the RECOVERY_VERIFYING probe locked down on an *unreadable* reread, where its
   sibling branch already reverses the whole plan and lets the confirming read
   decide.  Readable-and-foreign still locks down.
4: ``recover`` stamps every token with the pass's one ``now``; a permit issued
   after earlier gateway calls must also cover the time those calls may take.
"""
from __future__ import annotations

import unittest
from unittest import mock

from assurance.core.addressing import content_hash
from assurance.core.states import StopReason, TrialState
from assurance.core.timebase import parse_utc
from assurance.gateway.token import TokenKind
from assurance.gateway.write_gateway import GatewayOutcome, GatewayResult
from assurance.kernel.kernel import DEFAULT_LEASE_MS, contracted_lease_ms

from tests.assurance import test_kern_lifecycle as lifecycle
from tests.assurance.test_kern_recovery_reverses_own_partial_apply import (
    BASELINE, _Gateway, _two_axis_catalog,
)


class _Recording(_Gateway):
    def __init__(self, rereads):
        super().__init__(rereads)
        self.tokens = []

    def reverse_rollback(self, *, token):
        self.tokens.append(token)
        return super().reverse_rollback(token=token)

    def reread_configuration(self, *, token):
        self.tokens.append(token)
        return super().reread_configuration(token=token)


def _committed(kernel, candidate_id, case_id, *, stop, then=None):
    NOW = lifecycle.NOW
    trial_id = kernel.open_trial(candidate_id=candidate_id, case_id=case_id, now=NOW)
    kernel.advance_trial(trial_id, TrialState.VALIDATING, now=NOW)
    kernel.reserve(trial_id, now=NOW)
    kernel.advance_trial(trial_id, TrialState.RESERVED, now=NOW)
    plan = lifecycle.staged_plan(kernel, trial_id)
    plan["baselineConfig"] = dict(BASELINE)
    kernel.stage_actuation_plan(trial_id, plan=plan, now=NOW)
    kernel.advance_trial(trial_id, TrialState.PREPARING, now=NOW)
    ready = kernel.issue_token(trial_id, token_kind=TokenKind.READY, now=NOW)
    resource_id = kernel.reduced_state()["trials"][trial_id]["resourceId"]
    kernel.record_gateway_result(
        ready, GatewayResult(GatewayOutcome.ACKED,
                             observed_config_hash=content_hash(BASELINE),
                             evidence_refs=lifecycle.watchdog_arming_evidence(kernel, trial_id)),
        resource_id=resource_id, now=NOW)
    kernel.advance_trial(trial_id, TrialState.READY, now=NOW)
    kernel.record_commit_readiness(trial_id, watchdogs_armed=True,
                                   baseline_hash=content_hash(BASELINE), now=NOW)
    if then is not None:
        then()
    kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)
    if stop:
        kernel.advance_trial(trial_id, TrialState.STOPPING,
                             reason=StopReason.PARTIAL_APPLY, now=lifecycle.T1)
    else:
        kernel.issue_token(trial_id, token_kind=TokenKind.COMMIT, now=lifecycle.T1)
    return trial_id


def _kernel(gateway, shared_resource=False):
    with mock.patch.object(lifecycle, "catalog",
                           lambda epoch_id="epoch-1": _two_axis_catalog(
                               epoch_id, shared_resource=shared_resource)):
        kernel, _ = lifecycle.make_kernel(gateway=gateway)
    return kernel


class AnUnreadableProbeIsReversedNotLockedDown(unittest.TestCase):

    def _recover(self, rereads):
        gateway = _Recording(rereads)
        kernel = _kernel(gateway)
        trial_id = _committed(kernel, "candidate-1", "case-1", stop=False)
        self.assertIn(f"tx:{trial_id}", kernel._event_store.uncertain_transactions())
        kernel.recover(now=lifecycle.T2)
        resolution = kernel.reduced_state()["transactions"][f"tx:{trial_id}"]["resolution"]
        return gateway, resolution

    def test_an_unreadable_probe_reverses_the_plan(self):
        gateway, resolution = self._recover([None])
        self.assertIn("rollback", gateway.calls, "unreadable was read as foreign")
        self.assertEqual("ROLLED_BACK", resolution)

    def test_a_readable_foreign_probe_still_locks_down(self):
        gateway, resolution = self._recover([content_hash({"axisA": "9", "axisB": "0"})])
        # 2026-09-26: our own policies are withdrawn first; the lockdown stands.
        self.assertIn("rollback", gateway.calls)
        self.assertEqual("INCIDENT_LOCKDOWN", resolution)


class LaterPermitsInOnePassCoverTheCallsBeforeThem(unittest.TestCase):

    def test_the_second_transactions_reversal_outlives_the_first(self):
        gateway = _Recording([])
        kernel = _kernel(gateway)
        # The pass is blocked from opening a second trial while one is uncertain,
        # so both reach COMMIT_DECIDED first (as after a crash mid-pair).
        kernel.open_case(
            case_id="case-2", policy=lifecycle.policy(), active_vector="vector-1",
            usable_reserve={"harm-1": lifecycle.q(10).to_canonical_dict()},
            reserve_per_trial={"harm-1": lifecycle.q(4).to_canonical_dict()},
            evidence_cells=(), now=lifecycle.NOW)
        _committed(kernel, "candidate-1", "case-1", stop=True, then=lambda: _committed(
            kernel, "candidate-2", "case-2", stop=True))
        self.assertEqual(2, len(kernel._event_store.uncertain_transactions()))
        kernel.recover(now=lifecycle.T2)
        reversals = [t for t in gateway.tokens if t.token_kind is TokenKind.REVERSE_ROLLBACK]
        self.assertEqual(2, len(reversals))
        first, second = (parse_utc(t.lease_expiry) for t in reversals)
        self.assertGreater(second, first)
        # The second reversal's window spans the first reversal's whole window
        # (everything issued before it included) plus its own contracted price.
        state = kernel.reduced_state()
        trial = state["trials"][reversals[1].trial_id]
        own_price = contracted_lease_ms(state, trial, TokenKind.REVERSE_ROLLBACK)
        self.assertGreaterEqual(
            (second - first).total_seconds() * 1000, own_price,
            "the second permit does not cover the first reversal's window")

    def test_outside_recover_nothing_is_added(self):
        gateway = _Recording([])
        kernel = _kernel(gateway)
        trial_id = _committed(kernel, "candidate-1", "case-1", stop=True)
        state = kernel.reduced_state()
        trial = state["trials"][trial_id]
        token = kernel.issue_token(trial_id, token_kind=TokenKind.REVERSE_ROLLBACK,
                                   now=lifecycle.T2)
        lease = (parse_utc(token.lease_expiry) - parse_utc(lifecycle.T2)).total_seconds() * 1000
        self.assertEqual(contracted_lease_ms(state, trial, TokenKind.REVERSE_ROLLBACK), lease)


if __name__ == "__main__":
    unittest.main()
