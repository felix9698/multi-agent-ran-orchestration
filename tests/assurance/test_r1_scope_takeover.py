# SPDX-License-Identifier: MIT
"""A finished trial's live policy may be taken over, not only refused.

A supplementary axis leaves its sentinel by *creating* an A1 policy and returns
to it by *withdrawing* one, and the Kernel grants no STOP after a settlement
(``assurance/kernel/kernel.py`` issue_token lists no SETTLED_* state for
``TokenKind.STOP``).  So a settled policy stayed live and its binding kept the
scope, and every later transaction that named the same axis was refused with
"one active policy per (policy type, semantic scope)" -- freezing that axis for
the rest of the episode.

2026-09-16 priced it: of that day's eighteen episodes, all four that ended
CATALOG_EXHAUSTED and all three that ended KERNEL_TERMINATED did so on one of
these rejections (eight in all, every one on a @ue1 axis); the other eleven
carried none.

Ownership and liveness are two facts, and the fix records them apart: a
finalized binding still holds a live policy but its transaction is finished, so
a later one adopts that policy id and the write is an in-place update.  One
policy per scope still holds -- no second policy is ever created.

The adapter and journal are real; the policy port is injected.  No network, no
process, no equipment.
"""
from __future__ import annotations

import unittest

from assurance.gateway.commands import GatewayOperation, build_command
from assurance.gateway.r1_adapter import R1Adapter
from assurance.gateway.r1_binding_journal import BindingState, InMemoryR1BindingJournal
from assurance.gateway.write_gateway import GatewayOutcome
from assurance.gateway.plan import config_hash
from tests.assurance.action102_support import CAP, permit, plan_scope

SCOPE_KEY = "ueId=132"


class OnePolicyPort:
    """One policy per scope, exactly as the campaign-5 producer enforces it."""

    def __init__(self):
        self.policies = {}
        self.scope_owner = {}
        self.creates = []
        self.updates = []
        self.deleted = []
        self.next_id = 1

    def get_policy_type(self, policy_type):
        return {}

    def create_policy(self, ric, policy_type, body):
        scope = body["config"]["ueId"]
        owner = self.scope_owner.get(scope)
        if owner is not None:
            raise AssertionError(f"target scope already owned by policy {owner}")
        policy_id = f"policy-{self.next_id}"
        self.next_id += 1
        self.policies[policy_id] = dict(body)
        self.scope_owner[scope] = policy_id
        self.creates.append((policy_id, str(body["config"]["cap"])))
        return {"policyId": policy_id}

    def update_policy(self, policy_id, body):
        # The producer refuses a PUT whose revision OR fencing token is not
        # strictly greater than the stored one (producer.py, A1Conflict
        # "policy update requires a newer revision and fencingToken").  This
        # double used to accept any PUT, which is why it never caught the
        # retention defect: a retention case opens a fresh Kernel, its fences
        # restart at zero, and the live producer answered 409 to a token of 2
        # against a stored 32 (episodes c4630d27 and 0039b54c, same shape).
        # A double that is laxer than the thing it stands for cannot fail.
        current = self.policies[policy_id]["trace"]
        new_trace = body["trace"]
        # The revision rule always applies.  The fence rule applies only when
        # both bodies carry the field: this file's hermetic builder writes a
        # trace of ``{"revision": n}`` alone, while the live builder also writes
        # ``fencingToken`` -- a second way the double was weaker than the thing
        # it stands for.  Tests that want the fence checked put it in the body.
        if new_trace["revision"] <= current["revision"]:
            raise AssertionError(
                "policy update requires a newer revision and fencingToken")
        if "fencingToken" in new_trace and "fencingToken" in current:
            if new_trace["fencingToken"] <= current["fencingToken"]:
                raise AssertionError(
                    "policy update requires a newer revision and fencingToken")
        self.policies[policy_id] = dict(body)
        self.updates.append((policy_id, str(body["config"]["cap"])))

    def get_policy_status(self, policy_id):
        return {"observed": {CAP.axis: self.policies[policy_id]["config"]["cap"]},
                "enforceStatus": "ENFORCED", "aicStatus": {"episodeTerminal": True}}

    def delete_policy(self, policy_id):
        self.deleted.append(policy_id)
        body = self.policies.pop(policy_id)
        self.scope_owner.pop(body["config"]["ueId"], None)


class ScopeTakeoverTests(unittest.TestCase):

    def setUp(self):
        self.port = OnePolicyPort()
        self.journal = InMemoryR1BindingJournal()
        self.adapter = R1Adapter(
            policy_port=self.port,
            # The same contract the real builder has: the journal seeds the
            # revision and the builder returns last + 1.  A fixture pinning the
            # revision to 1 could not see a takeover send a revision the
            # producer had already stored -- the 2026-09-17 live refusal.
            policy_builder=lambda command, last_revision=0: {
                "config": {"ueId": "132", "cap": str(command["value"])},
                "trace": {"revision": int(last_revision) + 1}},
            near_rt_ric_id="near-rt-ric-hermetic",
            policy_type_id="102",
            readback_port=lambda **kwargs: {CAP.axis: self._live()},
            status_projection=lambda status: status.get("observed"),
            binding_journal=self.journal,
            scope_key=lambda body: f"ueId={body['config']['ueId']}",
            retain_binding_until_restore=True,
            clock=lambda: "2026-09-16T00:00:00Z",
        )

    def _live(self):
        owner = self.port.scope_owner.get("132")
        return CAP.baseline if owner is None else str(self.port.policies[owner]["config"]["cap"])

    def _dispatch(self, kind, operation, *, transaction, value=None, sequence=0):
        # The Kernel's fence is a per-resource counter that rises across every
        # transaction, not a constant: ``fence = state["fences"][resource] + 1``.
        # The default here used to be a fixed 2, so the harness could not tell a
        # rising fence from a reset one -- exactly the distinction the retention
        # defect lives in.
        self._fence = getattr(self, "_fence", 0) + 1
        token = permit(kind, config_hash({CAP.axis: CAP.baseline}), sequence, transaction_id=transaction,
                       trial_id=f"trial-{transaction}", fence=self._fence)
        extra = {"axis": CAP.axis, "value": value} if value is not None else {}
        return self.adapter.dispatch(token=token, command=build_command(
            token, operation, scope=plan_scope(), index=sequence, **extra))

    def _settle(self, transaction, value):
        """Validate, apply and finalize one transaction, as a settled trial does."""
        self.assertIs(GatewayOutcome.ACKED, self._dispatch(
            "PREPARE", GatewayOperation.VALIDATE, transaction=transaction, value=value).outcome)
        self.assertIs(GatewayOutcome.ACKED, self._dispatch(
            "COMMIT", GatewayOperation.APPLY, transaction=transaction, value=value,
            sequence=1).outcome)
        return self._dispatch("FINALIZE_LIVE", GatewayOperation.FINALIZE,
                              transaction=transaction, sequence=2)

    def test_a_live_transaction_still_owns_its_scope(self):
        # The invariant this whole mechanism protects: while the creating
        # transaction can still write, nobody else may touch the scope.
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-a", value="18")
        self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-a", value="18",
                       sequence=1)
        result = self._dispatch("PREPARE", GatewayOperation.VALIDATE,
                                transaction="tx-b", value="12")
        self.assertIs(GatewayOutcome.ACKED, result.outcome)  # VALIDATE creates nothing
        result = self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-b",
                                value="12", sequence=1)
        self.assertIs(GatewayOutcome.REJECTED, result.outcome)
        self.assertIn("still owned by transaction tx-a", result.detail)
        self.assertEqual([], self.port.updates)

    def test_a_settled_policy_is_taken_over_by_an_update_not_a_second_policy(self):
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx-a", "18").outcome)
        self.assertTrue(self.journal.binding_for("tx-a").finalized)

        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-b", value="12")
        result = self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-b",
                                value="12", sequence=1)
        self.assertIs(GatewayOutcome.ACKED, result.outcome)
        # One policy, updated in place -- the producer's 409 is never provoked.
        self.assertEqual(1, len(self.port.creates))
        self.assertEqual([("policy-1", "12")], self.port.updates)
        self.assertEqual("12", self._live())
        self.assertEqual("policy-1", self.journal.binding_for("tx-b").policy_id)
        self.assertIs(BindingState.RESTORED, self.journal.binding_for("tx-a").state)
        self.assertIn("taken over by transaction tx-b",
                      self.journal.binding_for("tx-a").detail)

    def test_the_adopter_is_found_however_the_transaction_ids_sort(self):
        """A transaction id that sorts *before* the owner's must still adopt.

        APPLY journals this transaction's own ``RESERVED`` record before asking
        whether there is a policy to adopt, and ``RESERVED`` holds the scope.
        Adoption used to ask ``scope_owner``, which returns whichever holder's
        transaction id sorts first -- so the adopter could find itself, decline,
        CREATE, and take the producer's 409.  Live on 2026-09-17 that is exactly
        what the retention trial did: its id carried a ``/retention`` segment and
        ``/`` sorts before the ``:trial:N`` of the transaction that owned the
        policy, so every retention ended ``PARTIAL_APPLY`` and no sitting ever
        qualified what it had attained.
        """
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx:b:trial:1", "18").outcome)
        adopter = "tx:b/retention:trial:1"
        self.assertLess(adopter, "tx:b:trial:1")  # the ordering that used to decide

        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction=adopter, value="12")
        result = self._dispatch("COMMIT", GatewayOperation.APPLY, transaction=adopter,
                                value="12", sequence=1)
        self.assertIs(GatewayOutcome.ACKED, result.outcome)
        self.assertEqual(1, len(self.port.creates))
        self.assertEqual([("policy-1", "12")], self.port.updates)
        self.assertEqual("policy-1", self.journal.binding_for(adopter).policy_id)

    def test_the_takeover_body_outranks_the_policy_it_adopts(self):
        """The PUT must carry a revision above the adopted policy's.

        The revision seed is type-wide, so it can lag what the producer holds
        for this scope.  Live on 2026-09-17 an adopting trial sent the same
        revision the adopted policy already had and the producer refused the
        PUT with "policy update requires a newer revision and fencingToken" --
        the takeover looked broken when only the number was.
        """
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx-a", "18").outcome)
        adopted = self.journal.binding_for("tx-a")
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-b", value="12")
        result = self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-b",
                                value="12", sequence=1)
        self.assertIs(GatewayOutcome.ACKED, result.outcome)
        taker = self.journal.binding_for("tx-b")
        self.assertGreater(taker.policy_revision, adopted.policy_revision,
                           "the adopting PUT must outrank the policy it adopts")

    def test_the_seed_lifts_only_where_a_live_policy_could_be_adopted(self):
        """An empty journal still starts at revision one.

        The takeover seed must not become a blanket bump: the first body a
        deployment ever sends carries revision one, and
        ``test_the_first_body_carries_revision_one_at_fence_zero`` pins it.
        Raising the seed unconditionally broke six tests on 2026-09-17.
        """
        self.assertEqual(0, self.adapter._last_revision("tx-fresh"))
        self._settle("tx-a", "18")
        adopted = self.journal.binding_for("tx-a")
        self.assertTrue(adopted.takeable)
        self.assertGreaterEqual(self.adapter._last_revision("tx-b"),
                                adopted.policy_revision)

    def test_the_adopter_rolls_back_to_what_was_live_not_to_the_sentinel(self):
        self._settle("tx-a", "18")
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-b", value="12")
        self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-b", value="12",
                       sequence=1)
        # tx-a's record carried the pre-tx-a baseline; tx-b must not inherit a
        # baseline that claims the axis was uncapped before it ran.
        self.assertEqual(dict(self.journal.binding_for("tx-a").baseline_config),
                         dict(self.journal.binding_for("tx-b").baseline_config))

    def test_a_takeover_survives_a_restart_because_the_flag_is_durable(self):
        self._settle("tx-a", "18")
        restarted = InMemoryR1BindingJournal(self.journal.snapshot())
        self.assertTrue(restarted.binding_for("tx-a").finalized)
        self.assertTrue(restarted.binding_for("tx-a").takeable)


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()


class ARetentionCaseRestartsTheFenceAndTheProducerRefusesIt(unittest.TestCase):
    """The open defect of 2026-09-17, pinned so the fix has something to turn green.

    A sitting's retention step opens a *second* case when the best control's
    candidate is already spent (``case_id + "/retention"``).  That case gets its
    own Kernel and its own ``MemoryEventStore``, and the Kernel's fence is a
    counter over that store -- ``fence = state["fences"].get(resource, -1) + 1``
    -- so it restarts near zero while the policy the retention has to adopt was
    last written under the search case's much higher fence.  The producer then
    refuses the PUT, and it is right to: its rule is "strictly greater revision
    AND fencingToken".

    Measured twice, identically:

        episode c4630d27  CREATE trial:8 fence 33  ->  UPDATE retention fence 2
        episode 0039b54c  CREATE trial:7 fence 32  ->  UPDATE retention fence 2

    The retention transaction is not a stale writer -- it runs *after* the
    search case finished -- so the fix is to carry the fence forward, not to
    relax the producer.  Two seams were traced and neither is small: reuse the
    search case's event store (then ``_admit_and_freeze`` must not re-freeze a
    frozen epoch), or seed the fences at Kernel construction (but fences are
    derived from ``TokenIssued`` events and the reducer refuses an old fence).

    Fixed 2026-09-17 without touching either of those seams.  The Kernel's fence
    is per-case and may restart; the *producer's* is per-policy and only rises,
    and the producer is the authority on it.  So the adapter reads the live
    policy's ``trace.fencingToken`` (``R1Adapter._live_fencing_token``) and the
    builder sends ``max(kernel fence, live fence + 1)``.  The Kernel's number
    still wins whenever it is ahead, which is every write that is not a
    cross-case takeover, so the normal path is byte-for-byte unchanged.

    The earlier version of this test fired at the port directly and so never
    crossed the seam that was broken; it pinned the producer's refusal, which
    was correct behaviour and was never the defect.
    """

    def setUp(self):
        self.port = OnePolicyPort()
        self.port.get_policy = lambda pid: self.port.policies[pid]
        self.journal = InMemoryR1BindingJournal()
        self.adapter = R1Adapter(
            policy_port=self.port,
            # Unlike the class above, this builder writes ``fencingToken`` -- the
            # live builder does, and the fence rule only engages when both
            # bodies carry the field.
            policy_builder=lambda command, last_revision=0, last_fencing_token=None: {
                "config": {"ueId": "132", "cap": str(command["value"])},
                "trace": {
                    "revision": int(last_revision) + 1,
                    "fencingToken": max(
                        int(command["fencingToken"]),
                        0 if last_fencing_token is None else int(last_fencing_token) + 1),
                }},
            near_rt_ric_id="near-rt-ric-hermetic",
            policy_type_id="102",
            readback_port=lambda **kwargs: {CAP.axis: self._live()},
            status_projection=lambda status: status.get("observed"),
            binding_journal=self.journal,
            scope_key=lambda body: f"ueId={body['config']['ueId']}",
            retain_binding_until_restore=True,
            clock=lambda: "2026-09-16T00:00:00Z",
        )

    _live = ScopeTakeoverTests._live
    _dispatch = ScopeTakeoverTests._dispatch
    _settle = ScopeTakeoverTests._settle

    def test_a_later_case_may_still_update_the_policy_it_adopts(self) -> None:
        # The search case runs the fence up, exactly as a real one does.
        self._fence = 32
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx:case:trial:8", "18").outcome)
        policy_id = self.journal.binding_for("tx:case:trial:8").policy_id
        stored = self.port.policies[policy_id]["trace"]["fencingToken"]
        self.assertGreaterEqual(stored, 33, "the search case must reach a high fence")

        # The retention case: a fresh Kernel, so its fence restarts near zero.
        self._fence = 0
        self._dispatch("PREPARE", GatewayOperation.VALIDATE,
                       transaction="tx:case/retention:trial:1", value="0")
        result = self._dispatch("COMMIT", GatewayOperation.APPLY,
                                transaction="tx:case/retention:trial:1", value="0",
                                sequence=1)
        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
        self.assertEqual([(policy_id, "0")], self.port.updates)
        self.assertGreater(self.port.policies[policy_id]["trace"]["fencingToken"], stored)

    def test_the_adopter_can_still_reverse_what_it_adopted(self) -> None:
        # 2026-09-25 board 604: a rebind case adopted the policy at a lifted fence
        # (27 over a Kernel fence of 2), then its STOP rewrote the baseline with
        # the Kernel's own fence 3 -- the adoption had made the binding its own,
        # no longer ``takeable``, so the live fence was no longer consulted.  409,
        # rollback PARTIAL_APPLY, incident lockdown.
        self._fence = 32
        self._dispatch("PREPARE", GatewayOperation.READ, transaction="tx:case:trial:8")
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx:case:trial:8", "18").outcome)
        policy_id = self.journal.binding_for("tx:case:trial:8").policy_id

        self._fence = 0
        # The read puts the adopter's own pre-trial observation (18) in its record,
        # so its reversal is a rewrite, not a withdrawal (see the class below).
        self._dispatch("PREPARE", GatewayOperation.READ, transaction="tx:case/rebind:trial:1")
        self._dispatch("PREPARE", GatewayOperation.VALIDATE,
                       transaction="tx:case/rebind:trial:1", value="6")
        self.assertIs(GatewayOutcome.ACKED, self._dispatch(
            "COMMIT", GatewayOperation.APPLY, transaction="tx:case/rebind:trial:1",
            value="6", sequence=1).outcome)
        adopted = self.port.policies[policy_id]["trace"]["fencingToken"]

        result = self._dispatch("STOP", GatewayOperation.UNDO,
                                transaction="tx:case/rebind:trial:1", value="18", sequence=2)
        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
        self.assertGreater(self.port.policies[policy_id]["trace"]["fencingToken"], adopted)
        self.assertEqual("18", self._live(), "the axis is back where the trial found it")

    def test_the_adopter_can_rewrite_the_baseline_twice_halt_then_undo(self) -> None:
        # 2026-09-29 board 918: a rebind case adopted at a lifted fence, its HALT rewrite
        # went out at cached fence + 1, the cache was not advanced, and the UNDO rewrite
        # drafted the same fence -- 409, INCIDENT_LOCKDOWN.
        self._fence = 32
        self._dispatch("PREPARE", GatewayOperation.READ, transaction="tx:case:trial:8")
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx:case:trial:8", "18").outcome)
        policy_id = self.journal.binding_for("tx:case:trial:8").policy_id
        self._fence = 0
        self._dispatch("PREPARE", GatewayOperation.READ, transaction="tx:case/rebind:trial:1")
        self._dispatch("PREPARE", GatewayOperation.VALIDATE,
                       transaction="tx:case/rebind:trial:1", value="6")
        self.assertIs(GatewayOutcome.ACKED, self._dispatch(
            "COMMIT", GatewayOperation.APPLY, transaction="tx:case/rebind:trial:1",
            value="6", sequence=1).outcome)
        halt = self._dispatch("STOP", GatewayOperation.HALT,
                              transaction="tx:case/rebind:trial:1", sequence=2)
        self.assertIs(GatewayOutcome.ACKED, halt.outcome, halt.detail)
        after_halt = self.port.policies[policy_id]["trace"]["fencingToken"]
        undo = self._dispatch("REVERSE_ROLLBACK", GatewayOperation.UNDO,
                              transaction="tx:case/rebind:trial:1", value="18", sequence=3)
        self.assertIs(GatewayOutcome.ACKED, undo.outcome, undo.detail)
        self.assertGreaterEqual(self.port.policies[policy_id]["trace"]["fencingToken"], after_halt)
        self.assertEqual("18", self._live())

    def test_the_kernel_fence_still_wins_when_it_is_ahead(self) -> None:
        # The lift is a floor, not a replacement: an ordinary second trial in
        # the same case must keep sending the Kernel's own rising fence.
        self._fence = 4
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx:case:trial:1", "18").outcome)
        policy_id = self.journal.binding_for("tx:case:trial:1").policy_id
        stored = self.port.policies[policy_id]["trace"]["fencingToken"]
        self._dispatch("PREPARE", GatewayOperation.VALIDATE,
                       transaction="tx:case:trial:2", value="12")
        self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx:case:trial:2",
                       value="12", sequence=1)
        sent = self.port.policies[policy_id]["trace"]["fencingToken"]
        self.assertEqual(self._fence, sent,
                         "an in-case write sends the Kernel's fence verbatim")
        self.assertGreater(sent, stored)

    def test_the_seed_is_read_from_the_shape_the_live_get_actually_returns(self) -> None:
        """The R1 GET answers with the A1 envelope, not the bare body.

        Measured against the live producer on 2026-09-17::

            {"nearRtRicId": "near-rt-ric-lics-lab-001",
             "policyTypeId": "AIC_UeDlPrbCap_1.0.0",
             "policyObject": {"config": {...},
                              "trace": {"fencingToken": 17, "revision": 4}}}

        The first version of the seed read ``policy["trace"]`` and therefore got
        ``None`` from every live policy -- inert on the bed, green in every test
        here, because this fixture had been handing back the bare body.  So the
        fixture now answers the way the producer does.
        """
        self.port.get_policy = lambda pid: {
            "nearRtRicId": "near-rt-ric-hermetic",
            "policyTypeId": "AIC_UeDlPrbCap_1.0.0",
            "policyObject": dict(self.port.policies[pid]),
        }
        self._fence = 32
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx:case:trial:8", "18").outcome)
        policy_id = self.journal.binding_for("tx:case:trial:8").policy_id
        stored = self.port.policies[policy_id]["trace"]["fencingToken"]

        self._fence = 0
        self._dispatch("PREPARE", GatewayOperation.VALIDATE,
                       transaction="tx:case/retention:trial:1", value="0")
        result = self._dispatch("COMMIT", GatewayOperation.APPLY,
                                transaction="tx:case/retention:trial:1", value="0",
                                sequence=1)
        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
        self.assertGreater(self.port.policies[policy_id]["trace"]["fencingToken"], stored)

    def test_a_port_that_cannot_be_read_does_not_fail_the_write(self) -> None:
        # A read must never fail a write: the fence falls back to the Kernel's,
        # which is the behaviour before the seed existed.
        def explode(pid):
            raise RuntimeError("producer unreachable")
        self._fence = 1
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx:case:trial:1", "18").outcome)
        self.port.get_policy = explode
        self._dispatch("PREPARE", GatewayOperation.VALIDATE,
                       transaction="tx:case:trial:2", value="12")
        result = self._dispatch("COMMIT", GatewayOperation.APPLY,
                                transaction="tx:case:trial:2", value="12", sequence=1)
        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)


class AWithdrawalCannotRestoreANonSentinelBaseline(unittest.TestCase):
    """Reversing a trial that returned an axis to its sentinel needs a *write*.

    ``A1 DELETE`` asks the actuator to roll back to the snapshot it took when
    the policy was created -- ``live_worker.py`` records ``entry["baseline"]``
    only ``if entry is None`` and later writes to the same policy refresh only
    ``notAfter``.  Scope takeover makes one policy span several trials, so:

        trial 4   adopts the policy, writes dlPrbCap@ue3 = 18
        trial 5   adopts it, writes 0 (the sentinel); its baseline is **18**
        trial 5   rolls back -> DELETE -> the actuator restores **0**

    and the Kernel judges ``PARTIAL_APPLY -- reversal did not restore the
    baseline configuration``.  Two live episodes ended ``RECOVERY_FAILURE``
    with retention blocked on exactly this shape (2026-09-16 222038 and
    2026-09-17 004935); the other thirty-six reversals of those two days had
    ``baseline == observed`` and were right to withdraw.
    """

    def setUp(self):
        self.port = OnePolicyPort()
        self.journal = InMemoryR1BindingJournal()
        self.adapter = R1Adapter(
            policy_port=self.port,
            policy_builder=lambda command, last_revision=0: {
                "config": {"ueId": "132", "cap": str(command["value"])},
                "trace": {"revision": int(last_revision) + 1}},
            near_rt_ric_id="near-rt-ric-hermetic", policy_type_id="102",
            readback_port=lambda **kwargs: {CAP.axis: self._live()},
            status_projection=lambda status: status.get("observed"),
            binding_journal=self.journal,
            scope_key=lambda body: f"ueId={body['config']['ueId']}",
            retain_binding_until_restore=True,
            clock=lambda: "2026-09-16T00:00:00Z",
        )

    _live = ScopeTakeoverTests._live
    _dispatch = ScopeTakeoverTests._dispatch
    _settle = ScopeTakeoverTests._settle

    def _run_to_the_reversal(self):
        # The creating trial: the axis was at its sentinel when it started.
        self._dispatch("PREPARE", GatewayOperation.READ, transaction="tx-create")
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx-create", "18").outcome)
        # The next trial reads first -- a live trial always does, and that read
        # is what puts *its own* pre-trial observation in the record.  Without
        # it the adapter falls back to inheriting the owner's baseline and the
        # fixture quietly stops reproducing the live shape: on the bed trial 4
        # recorded ``{"dlPrbCap@ue3": "0"}`` and trial 5 ``{"dlPrbCap@ue3":
        # "18"}``, two different baselines, which is the whole defect.
        self._dispatch("PREPARE", GatewayOperation.READ, transaction="tx-return")
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-return",
                       value=CAP.baseline)
        self.assertIs(GatewayOutcome.ACKED, self._dispatch(
            "COMMIT", GatewayOperation.APPLY, transaction="tx-return",
            value=CAP.baseline, sequence=1).outcome)
        record = self.journal.binding_for("tx-return")
        self.assertEqual({CAP.axis: "18"}, dict(record.baseline_config),
                         "the adopter's baseline is what was live when it validated")
        return self._dispatch("STOP", GatewayOperation.UNDO,
                              transaction="tx-return", value=CAP.baseline, sequence=2)

    def test_the_baseline_is_written_back_rather_than_withdrawn(self):
        result = self._run_to_the_reversal()
        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
        self.assertEqual([], self.port.deleted, "a withdrawal would land on the sentinel")
        self.assertEqual("18", self._live(), "the axis is back where the trial found it")
        self.assertIn("baseline rewritten", result.detail)

    def test_the_restored_policy_stays_live_and_adoptable(self):
        # Releasing the scope while the policy is live would make the next
        # transaction CREATE and meet "one active policy per scope".
        self._run_to_the_reversal()
        record = self.journal.binding_for("tx-return")
        self.assertIs(BindingState.BOUND, record.state)
        self.assertTrue(record.takeable, "a later trial must be able to adopt it")
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-next", value="12")
        result = self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-next",
                                value="12", sequence=1)
        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
        self.assertEqual([], self.port.creates[1:], "adopted, not created a second time")

    def test_a_sentinel_baseline_is_still_withdrawn(self):
        # The thirty-six reversals that already worked must not change: when the
        # creating transaction's baseline is the one to restore, DELETE reaches
        # it and is the right instrument.
        self._dispatch("PREPARE", GatewayOperation.READ, transaction="tx-create")
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx-create", "18").outcome)
        result = self._dispatch("STOP", GatewayOperation.UNDO,
                                transaction="tx-create", value=CAP.baseline, sequence=3)
        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
        self.assertEqual(1, len(self.port.deleted))
        self.assertNotIn("baseline rewritten", result.detail)


class DeclaringTheTypeOfAnInheritedPolicy(unittest.TestCase):
    """A policy id this process did not create must not be assumed to be steering.

    ``R1Client`` learns a type on create and otherwise falls back to the
    steering type, so a cap policy inherited through a takeover or a journal
    restore would have had its body validated against the wrong schema.
    """

    def test_the_client_default_is_steering_and_a_declaration_overrides_it(self):
        from oran.rapp.r1_client import R1Client, R1Error, STEERING_POLICY_TYPE
        client = R1Client.__new__(R1Client)
        client._policy_type_by_id = {}
        self.assertEqual(STEERING_POLICY_TYPE, client._policy_type_of("policy-1"))
        client.declare_policy_type("policy-1", "AIC_UeDlPrbCap_1.0.0")
        self.assertEqual("AIC_UeDlPrbCap_1.0.0", client._policy_type_of("policy-1"))
        with self.assertRaises(R1Error):
            client.declare_policy_type("policy-2", "not-a-frozen-type")

    def test_the_recording_wrapper_passes_the_declaration_through(self):
        # 2026-09-19 board 115202: the wrapper had no declare_policy_type, the
        # adapter's getattr found nothing, and an adopted priority policy was
        # validated against the steering schema by the rebind case's client.
        from oran.rapp.r1_client import R1Client
        from tools.g3ota.composition import RecordingPolicyPort
        client = R1Client.__new__(R1Client)
        client._policy_type_by_id = {}
        wrapped = RecordingPolicyPort(client)
        self.assertTrue(callable(getattr(wrapped, "declare_policy_type", None)))
        wrapped.declare_policy_type("policy-1", "AIC_SchedulerPriority_1.0.0")
        self.assertEqual("AIC_SchedulerPriority_1.0.0", client._policy_type_of("policy-1"))

    def test_a_takeover_declares_the_type_to_a_port_that_can_learn_it(self):
        declared = []
        port = OnePolicyPort()
        port.declare_policy_type = lambda pid, ptid: declared.append((pid, ptid))
        journal = InMemoryR1BindingJournal()
        adapter = R1Adapter(
            policy_port=port,
            policy_builder=lambda command: {
                "config": {"ueId": "132", "cap": str(command["value"])},
                "trace": {"revision": 1}},
            near_rt_ric_id="near-rt-ric-hermetic", policy_type_id="AIC_UeDlPrbCap_1.0.0",
            readback_port=lambda **kwargs: {CAP.axis: CAP.baseline},
            status_projection=lambda status: status.get("observed"),
            binding_journal=journal,
            scope_key=lambda body: f"ueId={body['config']['ueId']}",
            retain_binding_until_restore=True,
            clock=lambda: "2026-09-16T00:00:00Z")

        nonlocal_fence = [0]   # the Kernel's per-resource counter, modelled

        def run(transaction, kind, operation, value=None, sequence=0):
            nonlocal_fence[0] += 1
            token = permit(kind, config_hash({CAP.axis: CAP.baseline}), sequence,
                           transaction_id=transaction, trial_id=f"trial-{transaction}",
                           fence=nonlocal_fence[0])
            extra = {"axis": CAP.axis, "value": value} if value is not None else {}
            return adapter.dispatch(token=token, command=build_command(
                token, operation, scope=plan_scope(), index=sequence, **extra))

        run("tx-a", "PREPARE", GatewayOperation.VALIDATE, "18")
        run("tx-a", "COMMIT", GatewayOperation.APPLY, "18", 1)
        run("tx-a", "FINALIZE_LIVE", GatewayOperation.FINALIZE, sequence=2)
        self.assertEqual([], declared, "the creator already knows the type")

        run("tx-b", "PREPARE", GatewayOperation.VALIDATE, "12")
        run("tx-b", "COMMIT", GatewayOperation.APPLY, "12", 1)
        self.assertEqual([("policy-1", "AIC_UeDlPrbCap_1.0.0")], declared)


class RetentionCanWithdrawAPolicyASettledTrialLeftLive(unittest.TestCase):
    """Restoring a supplementary axis to its baseline is a withdrawal.

    A supplementary axis leaves its sentinel by CREATING a policy and returns to
    it by WITHDRAWING one, and after a settlement the Kernel grants the creating
    trial no STOP.  Retention is exactly the restore-to-baseline case, so it had
    no way to express what it needed: v4's first full episode (2026-09-17)
    ended "retained: C0 for T0 qualified=False :: C0 sets dlPrbCap@ue2 away from
    the value a settled policy holds, which first needs that live policy
    withdrawn", and two later proposals were rejected for the same reason.

    Adoption makes the withdrawal expressible by the transaction that is
    actually running.  The obligation is not weakened: the DELETE is still owed
    a baseline readback before the scope is released.
    """

    def setUp(self):
        self.port = OnePolicyPort()
        self.journal = InMemoryR1BindingJournal()
        self.adapter = R1Adapter(
            policy_port=self.port,
            policy_builder=lambda command: {
                "config": {"ueId": "132", "cap": str(command["value"])},
                "trace": {"revision": 1}},
            near_rt_ric_id="near-rt-ric-hermetic",
            policy_type_id="AIC_UeDlPrbCap_1.0.0",
            readback_port=lambda **kwargs: {CAP.axis: CAP.baseline},
            status_projection=lambda status: status.get("observed"),
            binding_journal=self.journal,
            scope_key=lambda body: f"ueId={body['config']['ueId']}",
            retain_binding_until_restore=True,
            clock=lambda: "2026-09-17T00:00:00Z")

    def _dispatch(self, kind, operation, *, transaction, value=None, sequence=0):
        self._fence = getattr(self, "_fence", 0) + 1
        token = permit(kind, config_hash({CAP.axis: CAP.baseline}), sequence,
                       transaction_id=transaction, trial_id=f"trial-{transaction}",
                       fence=self._fence)
        extra = {"axis": CAP.axis, "value": value} if value is not None else {}
        return self.adapter.dispatch(token=token, command=build_command(
            token, operation, scope=plan_scope(), index=sequence, **extra))

    def _settle(self, transaction, value):
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction=transaction, value=value)
        self._dispatch("COMMIT", GatewayOperation.APPLY, transaction=transaction,
                       value=value, sequence=1)
        return self._dispatch("FINALIZE_LIVE", GatewayOperation.FINALIZE,
                              transaction=transaction, sequence=2)

    def test_a_retention_transaction_withdraws_the_settled_policy(self):
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx-a", "18").outcome)
        self.assertEqual("18", self._live())

        # The retention transaction reserves the same scope, then asks for the
        # baseline -- which is a withdrawal, not an apply.
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-r",
                       value=CAP.baseline)
        result = self._dispatch("STOP", GatewayOperation.UNDO, transaction="tx-r",
                                value=CAP.baseline, sequence=1)
        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
        self.assertEqual(["policy-1"], self.port.deleted)
        self.assertEqual(CAP.baseline, self._live(), "the axis is back at its sentinel")

    def test_the_scope_is_still_held_until_the_baseline_is_read_back(self):
        self._settle("tx-a", "18")
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-r",
                       value=CAP.baseline)
        self._dispatch("STOP", GatewayOperation.UNDO, transaction="tx-r",
                       value=CAP.baseline, sequence=1)
        record = self.journal.binding_for("tx-r")
        self.assertIs(BindingState.RESTORE_PENDING, record.state,
                      "a DELETE response is not recovery")

    def test_nothing_is_adopted_while_the_creating_transaction_can_still_write(self):
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-a", value="18")
        self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-a",
                       value="18", sequence=1)     # applied but NOT finalized
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-r",
                       value=CAP.baseline)
        result = self._dispatch("STOP", GatewayOperation.UNDO, transaction="tx-r",
                                value=CAP.baseline, sequence=1)
        self.assertEqual([], self.port.deleted, "a live trial's policy is not another's to withdraw")
        # It still releases its OWN standing reservation, which is what lets a
        # later transaction reserve the same scope; what it must not do is reach
        # into a policy whose creating trial can still write.
        self.assertIn("no policy bound to this transaction", result.detail)
        self.assertIs(GatewayOutcome.ACKED, result.outcome)

    def _live(self):
        owner = self.port.scope_owner.get("132")
        return CAP.baseline if owner is None else str(self.port.policies[owner]["config"]["cap"])


class TheLiveFenceReadIsPaidOnceNotEveryWrite(
        ARetentionCaseRestartsTheFenceAndTheProducerRefusesIt):
    """살아 있는 fence 읽기는 **쓰기 경로에 있는 왕복**이다.

    2026-09-17 에 이 읽기를 매 쓰기마다 물리면서 보조 축 쓰기의 중앙값이
    7.3초 → 9.9초로 늘었고, 시행당 관측 창이 약 10초 길어졌으며, 판 두 개가
    남은 시행을 `LEASE_EXPIRED` 로 잃었다.

    프로듀서가 받아 준 값은 **우리가 보낸 값**이므로 첫 채택 때만 읽으면 된다.
    쓰기가 거절되면 우리 생각이 증명되지 않았으므로 그 항목만 지운다.
    """

    def _counting_port(self):
        reads = []
        raw = self.port.get_policy
        self.port.get_policy = lambda pid: (reads.append(pid), raw(pid))[1]
        return reads

    def test_a_second_write_to_the_same_policy_reads_nothing(self) -> None:
        self._fence = 32
        self.assertIs(GatewayOutcome.ACKED,
                      self._settle("tx:case:trial:8", "18").outcome)
        policy_id = self.journal.binding_for("tx:case:trial:8").policy_id
        reads = self._counting_port()

        self._fence = 0
        self._dispatch("PREPARE", GatewayOperation.VALIDATE,
                       transaction="tx:case/retention:trial:1", value="0")
        self._dispatch("COMMIT", GatewayOperation.APPLY,
                       transaction="tx:case/retention:trial:1", value="0", sequence=1)
        first = len(reads)
        self.assertGreaterEqual(first, 1, "첫 채택은 프로듀서에게 물어야 한다")

        # 같은 정책에 두 번째 쓰기: 이미 아는 값이므로 왕복이 없어야 한다.
        self._fence = 0
        self._dispatch("PREPARE", GatewayOperation.VALIDATE,
                       transaction="tx:case/retention:trial:2", value="6")
        self._dispatch("COMMIT", GatewayOperation.APPLY,
                       transaction="tx:case/retention:trial:2", value="6", sequence=1)
        self.assertEqual(first, len(reads),
                         "두 번째 쓰기가 또 읽었다: %r" % (reads,))
        # 그러고도 fence 는 계속 올라간다 -- 캐시가 정확성을 깎지 않는다.
        self.assertGreater(self.port.policies[policy_id]["trace"]["fencingToken"], 33)

    def test_a_refused_write_drops_what_we_only_believed(self) -> None:
        self._fence = 32
        self.assertIs(GatewayOutcome.ACKED,
                      self._settle("tx:case:trial:8", "18").outcome)
        policy_id = self.journal.binding_for("tx:case:trial:8").policy_id
        reads = self._counting_port()

        self._fence = 0
        # 정착까지 가야 scope 가 넘겨받을 수 있는 상태가 된다.
        self._settle("tx:case/retention:trial:1", "0")
        self.assertIn(str(policy_id), self.adapter._fence_cache)
        after_write = len(reads)

        # 프로듀서가 쓰기를 거절하면 우리가 보낸 값은 증명되지 않았다.
        working = self.port.update_policy

        def refuse(*_args, **_kwargs):
            raise RuntimeError("the producer refused")

        self.port.update_policy = refuse
        self._dispatch("PREPARE", GatewayOperation.VALIDATE,
                       transaction="tx:case/retention:trial:2", value="6")
        refused = self._dispatch("COMMIT", GatewayOperation.APPLY,
                                 transaction="tx:case/retention:trial:2",
                                 value="6", sequence=1)
        self.assertIsNot(GatewayOutcome.ACKED, refused.outcome, refused.detail)
        self.assertNotIn(str(policy_id), self.adapter._fence_cache,
                         "거절된 쓰기 뒤에는 캐시를 비워야 한다")

        self.port.update_policy = working
        # 다시 읽는 것은 인계 경로가 다시 열릴 때다.  여기서 고정하는 것은
        # **낡은 값이 살아남지 않는다**는 것 하나이고, 그것이 캐시가 정확성을
        # 깎지 않는 이유다.
        self.assertEqual(after_write, len(reads),
                         "거절 자체가 새 읽기를 만들지는 않는다")


class APolicyTheProducerNoLongerHasIsCreatedAgain(
        ARetentionCaseRestartsTheFenceAndTheProducerRefusesIt):
    """404 는 "프로듀서가 답했고 그것을 갖고 있지 않다" 는 뜻이다.

    라이브 정책 유효기간은 0.9~5.4분인데 판은 8분이라, 판 끝의 유지 시행은
    **프로듀서가 이미 버린 정책 id** 를 들고 있기 일쑤다. 2026-09-17 에 두 번
    나왔고 두 번째는 유지 시행을 통째로 잃었다:

        r1-cap@ue3 ... GET .../status returned 404: unknown policy a606fbb1-…
        dlPrbCap@ue3: REJECTED APPLY failed: PUT … → PARTIAL_APPLY

    없는 정책을 UPDATE 해서 성공할 수는 없고, 그것이 쥐고 있던 scope 는 비어 있다.
    정직한 복구는 **다시 만드는 것**이다. 404 에만 그렇게 한다 — 409 는 정책이
    있다는 뜻이고, 전송 실패는 아무것도 증명하지 않는다.
    """

    class _Gone(Exception):
        status = 404

    class _Conflict(Exception):
        status = 409

    def _adapter_refusing_update(self, error, *, gone=False):
        """프로듀서가 404 를 주는 상황은 **정책이 실제로 사라진** 상황이다.

        그러니 포트에서도 지워야 픽스처가 현실과 같다 -- 지우지 않으면 scope 가
        여전히 점유돼 있어 재생성이 거절되고, 그건 라이브에서 일어나지 않는 일이다.
        """
        self.adapter._refusal_errors = (type(error),)

        def fail(policy_id, *_args, **_kwargs):
            if gone:
                self.port.policies.pop(policy_id, None)
                for scope, owner in list(self.port.scope_owner.items()):
                    if owner == policy_id:
                        self.port.scope_owner.pop(scope, None)
            raise error

        self.port.update_policy = fail

    def test_a_404_on_update_creates_the_policy_again(self) -> None:
        self._fence = 32
        self._settle("tx:case:trial:8", "18")
        old = self.journal.binding_for("tx:case:trial:8").policy_id
        created_before = len(self.port.creates)

        self._adapter_refusing_update(self._Gone("unknown policy"), gone=True)
        self._fence = 0
        self._dispatch("PREPARE", GatewayOperation.VALIDATE,
                       transaction="tx:case/retention:trial:1", value="0")
        result = self._dispatch("COMMIT", GatewayOperation.APPLY,
                                transaction="tx:case/retention:trial:1",
                                value="0", sequence=1)

        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
        self.assertGreater(len(self.port.creates), created_before,
                           "새 정책이 만들어져야 한다")
        fresh = self.journal.binding_for("tx:case/retention:trial:1").policy_id
        self.assertNotEqual(old, fresh, "낡은 id 를 계속 들고 있으면 안 된다")
        self.assertNotIn(str(old), self.adapter._fence_cache,
                         "사라진 정책의 fence 를 기억하고 있으면 안 된다")

    def test_a_409_still_fails_because_the_policy_is_there(self) -> None:
        self._fence = 32
        self._settle("tx:case:trial:8", "18")
        created_before = len(self.port.creates)

        self._adapter_refusing_update(self._Conflict("newer revision required"))
        self._fence = 0
        self._dispatch("PREPARE", GatewayOperation.VALIDATE,
                       transaction="tx:case/retention:trial:1", value="0")
        result = self._dispatch("COMMIT", GatewayOperation.APPLY,
                                transaction="tx:case/retention:trial:1",
                                value="0", sequence=1)

        self.assertIsNot(GatewayOutcome.ACKED, result.outcome)
        self.assertEqual(created_before, len(self.port.creates),
                         "409 는 정책이 있다는 뜻이므로 새로 만들면 안 된다")


class AClosedTransactionReleasesWhatItNeverWrote(ScopeTakeoverTests):
    """2026-09-20, board 20260919T183101 trial 4: another participant's CREATE was
    refused, the rollback reversed only the written axes, and this participant's
    VALIDATE reservation blocked trial 5 ("still owned by ... trial:4 (RESERVED)")."""

    def _next_trial_can_write(self):
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-b", value="12")
        return self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-b",
                              value="12", sequence=1)

    def test_a_reservation_that_was_never_applied_is_released(self):
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-a", value="18")
        self.assertTrue(self.adapter.release_unapplied("tx-a"))
        self.assertIs(GatewayOutcome.ACKED, self._next_trial_can_write().outcome)

    def test_an_apply_refused_before_creation_is_released(self):
        from assurance.gateway.r1_adapter import R1Adapter  # noqa: F401 - same class
        self.adapter._refusal_errors = (RuntimeError,)  # noqa: SLF001
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-a", value="18")
        self.port.create_policy = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("409"))
        refused = self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-a",
                                 value="18", sequence=1)
        self.assertIs(GatewayOutcome.REJECTED, refused.outcome)
        self.assertTrue(self.adapter.release_unapplied("tx-a"))
        del self.port.create_policy
        self.assertIs(GatewayOutcome.ACKED, self._next_trial_can_write().outcome)

    def test_an_apply_that_may_have_written_stays_held(self):
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-a", value="18")
        self.port.create_policy = lambda *a, **k: (_ for _ in ()).throw(OSError("timeout"))
        unknown = self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-a",
                                 value="18", sequence=1)
        self.assertIs(GatewayOutcome.UNKNOWN, unknown.outcome)
        self.assertFalse(self.adapter.release_unapplied("tx-a"))       # fail-closed
        del self.port.create_policy
        self.assertIs(GatewayOutcome.REJECTED, self._next_trial_can_write().outcome)

    def test_a_live_policy_is_never_released_this_way(self):
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-a", value="18")
        self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-a", value="18",
                       sequence=1)
        self.assertFalse(self.adapter.release_unapplied("tx-a"))
        self.assertEqual(1, len(self.port.policies))
