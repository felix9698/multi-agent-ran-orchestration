"""The Measurement Collector protocol.

Owner lane: **KAGT**.  Signatures frozen by this design step.

Design section 4.5, in full:

    The Measurement Collector delivers raw counters, scope snapshots,
    timestamps, clock health, missing intervals, and trace hashes directly to
    the Kernel.  Agent summaries cannot substitute for raw measurements.

"Directly" is the load-bearing word and is why :data:`DELIVERS_TO_KERNEL_ONLY`
exists as an asserted constant rather than a docstring promise.  A collector
that routed through an advisory agent would let a component with no authority
decide which observations the Kernel ever sees -- and dropping an inconvenient
sample is a more effective way to change a verdict than arguing for one.

The collector is a *source*, not an evaluator.  It does not aggregate, it does
not compare against a target, it does not decide sufficiency and it does not
know what trial is running.  Windowing, estimators, minimum entity counts and
freshness live in the Measurement Contract and are applied by the Kernel; a
collector that pre-aggregated would make the contract's window geometry
unverifiable from the raw record.

Gaps are reported, never filled.  Design section 8 requires a conservative
contract-defined charge for a missing interval and forbids zero or last-value
substitution, so an implementation must emit
:class:`~assurance.collector.samples.MissingInterval` entries rather than
interpolate, and must report
:attr:`~assurance.collector.samples.ClockHealth.UNKNOWN` rather than assume a
healthy clock.
"""

from __future__ import annotations

from typing import Any, Callable, Final, Mapping, Protocol, Sequence, runtime_checkable

from assurance.collector.samples import ClockHealth, RawSample

__all__ = ["DELIVERS_TO_KERNEL_ONLY", "MeasurementCollector", "SampleSink"]

#: Structural marker asserted by the seam and boundary tests: samples go to the
#: Kernel and to nothing else.  Design section 4.5.
DELIVERS_TO_KERNEL_ONLY: Final[bool] = True


#: What the Kernel hands the collector to receive samples.  Typed as a callable
#: rather than as the Kernel itself so a collector cannot reach any other
#: Kernel method -- it can deliver a sample and do nothing else.
SampleSink = Callable[[RawSample], None]


@runtime_checkable
class MeasurementCollector(Protocol):
    """A source of raw observations for the Kernel."""

    def bind_sink(self, sink: SampleSink) -> None:
        """Bind the Kernel's ingest callback.

        Signature frozen; body owned by lane **KAGT**.

        Exactly one sink.  Binding a second must raise: two sinks would mean
        two consumers of the same sample stream, and the Kernel could no
        longer claim its event stream is the complete record of what was
        observed.

        The sink is the Kernel's
        :meth:`~assurance.kernel.kernel.AssuranceKernel.ingest_raw_sample`,
        adapted to a callable.  No implementation may accept a sink supplied
        by an advisory agent.
        """
        ...

    def poll(self, *, now: str) -> Sequence[RawSample]:
        """Return samples observed since the previous poll.

        Signature frozen; body owned by lane **KAGT**.

        Must return every sample, including ones the collector believes are
        useless -- a sample from an unhealthy clock, a window full of gaps, a
        counter that did not move.  Sufficiency is decided by the Kernel
        against the Measurement Contract
        (:class:`~assurance.core.axes.MeasurementSufficiency`); a collector
        that filtered would be making that decision without the contract.

        Samples are returned in per-counter sequence order.  A gap in the
        sequence is left as a gap: repairing it here would hide a delivery
        loss that the Kernel needs to charge conservatively.
        """
        ...

    def clock_health(self) -> ClockHealth:
        """The collector's current clock health.

        Signature frozen; body owned by lane **KAGT**.

        Must return :attr:`~assurance.collector.samples.ClockHealth.UNKNOWN`
        rather than a guess when the source does not report synchronisation
        state.  Fail-closed: an unstated clock makes correlation unsafe, and
        the Kernel needs to know that rather than infer health from silence.
        """
        ...

    def scope_snapshot(self) -> Mapping[str, str]:
        """The entity identity the collector is currently observing.

        Signature frozen; body owned by lane **KAGT**.

        Read at observation time and stamped onto every sample, so a
        membership change mid-window is visible in the raw record instead of
        silently changing what an average was over.
        """
        ...

    def describe_source(self) -> Mapping[str, Any]:
        """Provenance of this collector for the run's environment record.

        Signature frozen; body owned by lane **KAGT**.

        Feeds design section 13's "environment, software, contract, model,
        topology, and hardware provenance".  Must contain no credential --
        the export includes it verbatim.
        """
        ...
