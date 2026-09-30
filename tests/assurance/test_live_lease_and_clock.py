"""The two live-runtime mechanisms the QoS families needed, under test.

Both were added to make a long-hold objective drivable and both were first
written in the wrong place, so the tests here are as much about *where* the
rule lives as about what it computes.

``lease``
    A permit's lease is the only statement of how long an authorisation
    lasts.  The A1 policy derived from it must therefore carry exactly the
    Kernel-recorded expiry -- never a later one computed downstream, which
    would leave a policy enforcing after the permit that authorised it had
    run out.  The hold genuinely needs a longer window than a point-in-time
    write, so that need is priced at issuance and a permit too short for the
    contracted window is refused rather than stretched.

``clock health``
    ``SYNCHRONISED`` is a claim that a sample sits on the Kernel's timebase.
    Freshness does not establish it: a producer whose clock is wrong emits
    timestamps that look recent.  Promotion therefore needs a same-host
    basis -- either agreement between the producer's claim and this host's
    record of when the file arrived, or a timestamp this host generated
    itself.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from assurance.collector.samples import ClockHealth, RawSample
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.timebase import format_utc, parse_utc
from assurance.gateway.token import TokenKind
from assurance.kernel.kernel import DEFAULT_LEASE_MS, R1_CALL_BOUND_MS, R1_UNDO_STEP_BOUND_MS, contracted_lease_ms
from assurance.live.objective_runtime import (
    _corroborated_by_arrival,
    _receiver_arrival,
    _receiver_generated,
)
from unittest import mock

from tools.g3ota import composition
from tools.g3ota.composition import LiveDriverError

from tests.assurance import test_live_pin_to_cell_driver as driver_tests
from tests.assurance.test_live_pin_to_cell_driver import LiveRunFixture


NOW = "2026-08-28T12:00:00.000000Z"


def _state(*, hold_ms: int, action_deadline_ms: int = 10_000) -> dict:
    """The shape the Kernel reduces admitted contracts into.

    Written out rather than driven through a full trial because the rule
    under test is a pure function of the frozen contracts, and a synthetic
    epoch keeps the arithmetic visible.
    """
    return {
        "epochs": {
            "epoch-1": {
                "targetContractHashes": {"target-1": "hash-target"},
                "measurementContractHashes": {"m-min": "hash-measurement"},
                "harmContractHashes": {"harm-1": "hash-harm"},
            }
        },
        "contracts": {
            "hash-target": {"family": "TargetContract", "body": {"holdMs": 0}},
            "hash-measurement": {
                "family": "MeasurementContract",
                "body": {"holdMs": hold_ms},
            },
            "hash-harm": {
                "family": "HarmContract",
                "body": {"bounds": [{"enforcedTimeoutMs": action_deadline_ms}]},
            },
        },
    }


TRIAL = {"epochId": "epoch-1", "targetRef": "target-1"}


class TheLeaseIsPricedWhereItIsGranted(unittest.TestCase):
    """Finding 1: the window belongs at issuance, not downstream."""

    def setUp(self) -> None:
        self.lease_ms = staticmethod(contracted_lease_ms).__func__
        self.default = DEFAULT_LEASE_MS

    def test_a_point_in_time_operation_keeps_the_default_lease(self) -> None:
        """With no staged plan there is nothing to read back."""
        for kind in (TokenKind.READY, TokenKind.FINALIZE_LIVE):
            with self.subTest(token_kind=kind):
                self.assertEqual(self.default,
                                 self.lease_ms(_state(hold_ms=120_000), TRIAL, kind))

    def test_prepare_and_reread_are_priced_per_participant_read(self) -> None:
        """Board 758 (2026-09-24): a 30 s PREPARE expired while the gateway read
        the joint plan's participants back, 60.1 s, and the case ended."""
        staged = dict(TRIAL, planPrefixHashes=["h"] * 10)       # 9 staged steps
        for kind in (TokenKind.PREPARE, TokenKind.READY, TokenKind.CONFIGURATION_REREAD,
                     TokenKind.RECOVERY_CONFIRM, TokenKind.FINALIZE_LIVE):
            with self.subTest(token_kind=kind):
                lease = self.lease_ms(_state(hold_ms=120_000), staged, kind)
                self.assertEqual((9 + 1) * R1_CALL_BOUND_MS, lease)
                self.assertGreater(lease, 60_100, "must cover the prepare that expired")
        self.assertEqual(self.default, self.lease_ms(_state(hold_ms=0), TRIAL,
                                                     TokenKind.PREPARE))

    def test_stop_and_reversal_are_priced_per_staged_step(self) -> None:
        """One strictest action deadline per step, plus one for the confirming read.

        2026-09-23, two days of live boards: 208 reversals, p50 0.0 s, p99 40.5 s,
        max 81.3 s -- the slow ones at whole multiples of the 20 s R1 call timeout,
        serially across a 9-step joint plan.  Four outlived a flat 30 s permit and
        the Kernel refused them as LEASE_EXPIRED, ending the case.
        """
        staged = dict(TRIAL, planPrefixHashes=["h"] * 10)       # 9 staged steps
        for kind in (TokenKind.STOP, TokenKind.REVERSE_ROLLBACK):
            with self.subTest(token_kind=kind):
                lease = self.lease_ms(_state(hold_ms=120_000), staged, kind)
                # 2026-09-23 audit: a step is priced by the R1 call bound it runs
                # into, not by the 10 s action deadline (see R1_UNDO_STEP_BOUND_MS).
                self.assertEqual((9 + 1) * R1_UNDO_STEP_BOUND_MS, lease)
                self.assertGreater(lease, 81_300, "must cover the slowest reversal observed")

    def test_one_reversal_step_covers_a_steering_hand_back(self) -> None:
        """UPDATE + corroborated readback + independent read + DELETE, each up to
        the 20 s R1 bound, plus the producer gate's 4 waits of 2 s."""
        self.assertGreaterEqual(R1_UNDO_STEP_BOUND_MS, 4 * 20_300 + 4 * 2_000)
        staged = dict(TRIAL, planPrefixHashes=["h", "h"])       # 1 staged step
        self.assertEqual(2 * R1_UNDO_STEP_BOUND_MS,
                         self.lease_ms(_state(hold_ms=0), staged, TokenKind.REVERSE_ROLLBACK))

    def test_the_read_and_confirm_after_a_reversal_span_the_reversal(self) -> None:
        """Board 459 (2026-09-23): ``recover`` stamps every token with one ``now``,
        so the reread and confirmation after a 61 s reversal arrived with 30 s
        permits already expired and an ACKED rollback ended in a lockdown."""
        staged = dict(TRIAL, planPrefixHashes=["h"] * 10,
                      state="RECOVERY_VERIFYING")
        reversal = self.lease_ms(_state(hold_ms=0), staged, TokenKind.REVERSE_ROLLBACK)
        for kind in (TokenKind.CONFIGURATION_REREAD, TokenKind.RECOVERY_CONFIRM):
            with self.subTest(token_kind=kind):
                self.assertEqual(reversal + self.default,
                                 self.lease_ms(_state(hold_ms=0), staged, kind))

    def test_a_short_plan_never_drops_the_reversal_below_the_default(self) -> None:
        one_step = dict(TRIAL, planPrefixHashes=["h", "h"])
        self.assertGreaterEqual(self.lease_ms(_state(hold_ms=0), one_step,
                                              TokenKind.REVERSE_ROLLBACK), self.default)

    def test_a_reversal_with_no_staged_plan_or_no_deadline_keeps_the_default(self) -> None:
        """Fail closed, as the COMMIT pricing does."""
        self.assertEqual(self.default, self.lease_ms(_state(hold_ms=0), TRIAL,
                                                     TokenKind.REVERSE_ROLLBACK))
        staged = dict(TRIAL, planPrefixHashes=["h"] * 10)
        self.assertEqual(self.default, self.lease_ms(
            _state(hold_ms=0, action_deadline_ms=0), staged, TokenKind.STOP))

    def test_a_commit_lease_covers_the_hold_and_the_two_actions_after_it(
        self,
    ) -> None:
        """120 s hold + 2 x 10 s action deadline = the reread and the finalise."""
        self.assertEqual(
            self.lease_ms(_state(hold_ms=120_000), TRIAL, TokenKind.COMMIT),
            140_000,
        )

    def test_a_staged_commit_also_covers_reading_the_apply_back(self) -> None:
        """Board 762 (2026-09-24): hold + 2 x 10 s expired while the steering apply
        was still being read back; each participant read is one R1 call bound."""
        staged = dict(TRIAL, planPrefixHashes=["h"] * 10)       # 9 staged steps
        self.assertEqual(
            self.lease_ms(_state(hold_ms=120_000), staged, TokenKind.COMMIT),
            140_000 + (9 + 1) * R1_CALL_BOUND_MS,
        )

    def test_a_short_hold_does_not_shrink_the_lease_below_the_default(self) -> None:
        """The PIN_TO_CELL regression case: 3 s hold, unchanged 30 s lease.

        Stated because the fix must not quietly alter the objective that was
        already driven OTA under the old constant.
        """
        self.assertEqual(
            self.lease_ms(_state(hold_ms=3_000), TRIAL, TokenKind.COMMIT),
            self.default,
        )

    def test_an_unresolvable_contract_leaves_the_default_in_place(self) -> None:
        """Fail closed: the short lease is then refused downstream."""
        blank = {"epochs": {}, "contracts": {}}
        self.assertEqual(
            self.lease_ms(blank, TRIAL, TokenKind.COMMIT), self.default
        )

    def test_a_harm_contract_naming_no_deadline_leaves_the_default(self) -> None:
        state = _state(hold_ms=120_000, action_deadline_ms=0)
        self.assertEqual(
            self.lease_ms(state, TRIAL, TokenKind.COMMIT), self.default
        )


class ThePolicyValidityIsThePermitAndNotASecondClock(LiveRunFixture):
    """Finding 1, end to end: what the Kernel recorded is what ships."""

    def test_the_composed_policy_expiry_equals_the_kernel_lease(self) -> None:
        self.run_case()

        leases = {
            envelope.payload["transactionId"]: envelope.payload["leaseExpiry"]
            for envelope in self.runtime.event_store.iterate()
            if envelope.event_kind == "TokenIssued"
            and envelope.payload.get("tokenKind") == TokenKind.COMMIT.value
        }
        self.assertTrue(leases, "no COMMIT permit was recorded")
        bodies = [
            record["policyObject"]
            for record in self.testbed.policies.values()
            if record.get("policyObject")
        ]
        self.assertTrue(bodies, "no policy was created")
        for body in bodies:
            with self.subTest(policy=body["trace"]["correlationId"]):
                self.assertIn(body["validity"]["expiresAt"], set(leases.values()))

    def test_a_validate_is_not_held_to_the_hold_window(self) -> None:
        """The side-effect-free command composes under a short permit.

        ``VALIDATE`` builds a body to check it and discards it, and its permit
        is deliberately short because nothing survives it.  Holding it to the
        hold-length window would refuse every long-hold objective at PREPARE,
        before the trial that needs the window ever began -- which is exactly
        what a first, over-broad version of this guard did on real radio.
        """
        seen: list = []
        original = composition.LivePolicyBuilder

        class Capturing(original):
            def __post_init__(self) -> None:
                super().__post_init__()
                seen.append(self)

            def __call__(self, command):
                if str(command.get("operation")) == "VALIDATE":
                    self.validate_command = dict(command)
                return super().__call__(command)

        with mock.patch.object(driver_tests, "LivePolicyBuilder", Capturing):
            self.run_case()

        builder = next(b for b in seen if getattr(b, "validate_command", None))
        builder.contracts = dict(
            builder.contracts,
            measurement_min=replace(
                builder.contracts["measurement_min"], hold_ms=86_400_000
            ),
        )

        body = builder(builder.validate_command)

        self.assertIn("validity", body)

    def test_a_restore_pins_the_baseline_on_a_fresh_window_and_newer_revision(self) -> None:
        """2026-09-19: the applied policy's lease had run out before HALT, so a
        restore body carrying it would be stored EXPIRED and never enacted."""
        seen: list = []
        original = composition.LivePolicyBuilder

        class Capturing(original):
            def __post_init__(self) -> None:
                super().__post_init__()
                seen.append(self)

            def __call__(self, command):
                if str(command.get("operation")) == "APPLY":
                    self.apply_command = dict(command)
                return super().__call__(command)

        with mock.patch.object(driver_tests, "LivePolicyBuilder", Capturing):
            self.run_case()
        builder = next(b for b in seen if getattr(b, "apply_command", None))
        applied = builder(builder.apply_command)
        home = str(builder.deployment.home_nci)
        restore = dict(builder.apply_command, operation="HALT", value=home)
        body = builder(restore)
        self.assertEqual(body["steeringObjective"]["actionEnvelope"]["allowedCells"],
                         [builder.deployment.topology.cell_object(int(home))])
        self.assertGreater(body["trace"]["policyRevision"], applied["trace"]["policyRevision"])
        self.assertNotEqual(body["trace"]["idempotencyKey"], applied["trace"]["idempotencyKey"])
        self.assertGreater(body["validity"]["expiresAt"], applied["validity"]["notBefore"])
        with self.assertRaises(Exception):
            builder(dict(builder.apply_command, value=home))   # an APPLY still needs its candidate

    def test_a_hold_longer_than_the_issued_lease_is_refused(self) -> None:
        """Not silently extended.  The refusal names the window it cannot cover.

        The divergence is manufactured the way it would really arise: the
        epoch is frozen and the permit issued for the hold those contracts
        state, and only then does the composing side come to believe in a
        longer hold.  The ``APPLY`` command that installed a policy a moment
        ago is replayed against the stretched belief, so the only thing that
        changed is the window being claimed -- and the builder refuses it
        rather than shipping a policy that would outlive its authorisation.
        """
        seen: list = []
        original = composition.LivePolicyBuilder

        class Capturing(original):
            def __post_init__(self) -> None:
                super().__post_init__()
                seen.append(self)

            def __call__(self, command):
                if str(command.get("operation")) == "APPLY":
                    self.apply_command = dict(command)
                return super().__call__(command)

        with mock.patch.object(driver_tests, "LivePolicyBuilder", Capturing):
            self.run_case()

        builder = next(b for b in seen if getattr(b, "apply_command", None))
        command = builder.apply_command
        # Sanity: the permit really does cover the contracted hold today.
        builder(command)

        builder.contracts = dict(
            builder.contracts,
            measurement_min=replace(
                builder.contracts["measurement_min"], hold_ms=86_400_000
            ),
        )
        with self.assertRaises(LiveDriverError) as raised:
            builder(command)

        message = str(raised.exception)
        self.assertIn("outlive its authorisation", message)
        self.assertIn("the permit's lease expires at", message)


_SAMPLE_ID = hashlib.sha256(b"sample").hexdigest()
_TRACE_HASH = hashlib.sha256(b"trace").hexdigest()


def _sample(observed_at: str, *, cadence_ms: int = 60_000,
            clock_health: ClockHealth = ClockHealth.UNKNOWN) -> RawSample:
    return RawSample(
        sample_id=_SAMPLE_ID,
        counter_id="RRU.PrbDl",
        value=TypedQuantity(10.0, "percent", Provenance.MEASURED, _SAMPLE_ID),
        scope_snapshot={"cellId": "NRCellDU-1"},
        observed_at=observed_at,
        cadence_ms=cadence_ms,
        clock_health=clock_health,
        trace_hash=_TRACE_HASH,
        sequence=0,
    )


class OnlyASameHostBasisPromotesAClock(unittest.TestCase):
    """Finding 2: freshness is not synchronisation."""

    def test_a_producer_claim_agreeing_with_arrival_is_promoted(self) -> None:
        arrival = parse_utc(NOW)
        sample = _sample(format_utc(arrival - timedelta(seconds=5)))

        promoted = _corroborated_by_arrival((sample,), NOW)

        self.assertEqual(promoted[0].clock_health, ClockHealth.SYNCHRONISED)

    def test_a_fresh_looking_but_uncorroborated_claim_is_not_promoted(self) -> None:
        """The case the rejected rule got wrong.

        This timestamp is minutes from now -- it would have passed a "within
        two cadences of local time" test at a 60 s cadence.  What condemns it
        is that the file carrying it did not arrive anywhere near the instant
        it claims, so the producer's clock is not ours.
        """
        arrival = parse_utc(NOW)
        sample = _sample(format_utc(arrival - timedelta(seconds=110)))

        promoted = _corroborated_by_arrival((sample,), NOW)

        self.assertEqual(promoted[0].clock_health, ClockHealth.UNKNOWN)

    def test_a_producer_clock_running_ahead_is_not_promoted(self) -> None:
        arrival = parse_utc(NOW)
        sample = _sample(format_utc(arrival + timedelta(seconds=180)))

        promoted = _corroborated_by_arrival((sample,), NOW)

        self.assertEqual(promoted[0].clock_health, ClockHealth.UNKNOWN)

    def test_without_an_arrival_record_nothing_is_promoted(self) -> None:
        sample = _sample(NOW)

        self.assertEqual(
            _corroborated_by_arrival((sample,), None)[0].clock_health,
            ClockHealth.UNKNOWN,
        )

    def test_a_source_that_reported_trouble_is_never_overruled(self) -> None:
        """An O1 ``suspect`` flag outranks our corroboration."""
        sample = _sample(NOW, clock_health=ClockHealth.DRIFTING_OUT_OF_BOUND)

        promoted = _corroborated_by_arrival((sample,), NOW)

        self.assertEqual(promoted[0].clock_health, ClockHealth.DRIFTING_OUT_OF_BOUND)

    def test_a_timestamp_this_host_generated_is_already_our_timebase(self) -> None:
        """KPM stamps arrival from this host's clock, so there is no second clock."""
        sample = _sample("2020-01-01T00:00:00.000000Z", cadence_ms=1000)

        promoted = _receiver_generated((sample,))

        self.assertEqual(promoted[0].clock_health, ClockHealth.SYNCHRONISED)

    def test_receiver_generated_still_does_not_overrule_a_reported_fault(self) -> None:
        sample = _sample(NOW, clock_health=ClockHealth.DRIFTING_OUT_OF_BOUND)

        self.assertEqual(
            _receiver_generated((sample,))[0].clock_health,
            ClockHealth.DRIFTING_OUT_OF_BOUND,
        )


class TheArrivalInstantComesFromThisHost(unittest.TestCase):
    """Why first sight and not the file's own timestamp."""

    def test_arrival_is_read_from_this_hosts_clock(self) -> None:
        before = datetime.now(timezone.utc)
        arrival = parse_utc(_receiver_arrival())
        after = datetime.now(timezone.utc)

        self.assertLessEqual(before, arrival)
        self.assertLessEqual(arrival, after)

    def test_arrival_does_not_come_from_a_rewritable_file_stamp(self) -> None:
        """``mtime`` is not an arrival record; anything re-copying rewrites it.

        Observed in this deployment: the PM mirror re-copies files every two
        seconds, so a batch of files describing periods minutes apart all
        carried one identical ``mtime`` -- the instant of the last copy.  A
        corroboration built on that would be comparing the producer's clock
        against a value a third process controls.  This pins the choice: the
        arrival stamp must not move when the file's mtime does.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pm.xml"
            path.write_text("<measCollecFile/>", encoding="utf-8")
            stale = parse_utc("2020-01-01T00:00:00.000000Z").timestamp()
            os.utime(path, (stale, stale))

            arrival = parse_utc(_receiver_arrival())

            self.assertGreater(arrival.year, 2020)


if __name__ == "__main__":
    unittest.main()
