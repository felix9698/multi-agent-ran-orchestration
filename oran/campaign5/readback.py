"""Per-family readback ports on the corroborated-readback discipline.

Design section 9 and ``CorroboratedServingCellReadback``
(``assurance/live/pin_to_cell_driver.py``) both refuse to call a write verified
unless **two independent observations agree**: the producer's own policy status
(``readback.result == "VERIFIED"``) *and* a fresh re-read of the E2SM-KPM
configuration counter for that scope.  Either half alone yields ``None``, which
the Write Gateway turns into ``UNKNOWN`` -- an acceptance is never an effect.

The configuration counters (``RAN.UE.DlPrbCap``, ``RAN.UE.PfWeight``,
``RAN.Cell.DlMcsBounds``, ``RAN.Cell.TxAttenuationDb``) **do not exist in the
deployed binary yet** (another agent patches the gNB).  So the KPM reader
degrades honestly: when the counter is absent from the stream it returns
``None``, and this port then reports ``None`` -- NOT_AVAILABLE/UNKNOWN -- rather
than fabricating a value or trusting the producer's ACK alone.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, Mapping, Optional, Protocol

from .families import Campaign5Family

__all__ = [
    "AbsentCounterReader",
    "DictKpmConfigReader",
    "PowerReadbackUnavailable",
    "CorroboratedConfigReadback",
    "KpmConfigReader",
    "make_status_projection",
]


class KpmConfigReader(Protocol):
    """Reads the family's configuration counter for a scope, or ``None``."""

    def read(
        self, counter_name: str, scope: Mapping[str, Any]
    ) -> Optional[Mapping[str, Any]]: ...


class AbsentCounterReader:
    """The deployed binary today: the configuration counter is not advertised.

    Every read is ``None`` -- NOT_AVAILABLE.  This is the correct, honest state
    until the gNB patch adds the counter, its KPM subscription requests it, and
    it appears in the tailed JSONL.  With this reader the official path cannot
    even establish the baseline configuration, so it never reports a fabricated
    effect: prepare answers ``UNKNOWN`` and no write is staged.
    """

    def read(self, counter_name: str, scope: Mapping[str, Any]) -> None:
        del counter_name, scope
        return None


class PowerReadbackUnavailable(AbsentCounterReader):
    """Honest power-readback dependency until the KPM JSONL wire record has labels.

    ``RAN.Cell.TxAttenuationDb`` is emitted by the gNB as three ``distBinX``
    components (attenuation, applied gain, and range maximum).  The current
    JSONL adapter preserves only counter name/type/value, so those components
    collapse to one counter identity and cannot be safely reconstructed.  Do
    not use arrival order as an implicit label.  Until the out-of-tree
    subscription/JSONL contract transports the component label, this reader
    reports no observation and the gateway consequently reports ``UNKNOWN``.
    """


class DictKpmConfigReader:
    """Hermetic reader returning the configured value leaves keyed by scope.

    Models a gNB that *does* advertise the configuration counter: it returns the
    observed value leaves (e.g. ``{"maxDlPrbs": 12}``) for a scope, or ``None``
    when the stream has no fresh indication for it.  This is the independent
    second observation the corroborated readback compares against the producer's
    own status.
    """

    def __init__(self, samples: Optional[Mapping[str, Mapping[str, Any]]] = None) -> None:
        self._samples: Dict[str, Mapping[str, Any]] = dict(samples or {})

    @staticmethod
    def _key(counter_name: str, scope: Mapping[str, Any]) -> str:
        parts = [counter_name] + [f"{name}={scope[name]}" for name in sorted(scope)]
        return "|".join(parts)

    def publish(self, counter_name: str, scope: Mapping[str, Any],
                value_leaves: Mapping[str, Any]) -> None:
        self._samples[self._key(counter_name, scope)] = dict(value_leaves)

    def read(self, counter_name: str, scope: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        observed = self._samples.get(self._key(counter_name, scope))
        return dict(observed) if observed is not None else None


def make_status_projection(
    family: Campaign5Family,
) -> Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]:
    """Project the VERIFIED observed configuration out of a policy status.

    Mirrors ``project_verified_readback`` but for this family's observed
    quantity: ``None`` unless ``aicStatus.readback.result == "VERIFIED"`` and the
    ``observed<...>`` object is present, exactly the distinction the status
    schema draws between an ACK (``resultIsEffectEvidence == false``) and an
    observed effect.
    """

    def project(status: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        aic = status.get("aicStatus") if isinstance(status, Mapping) else None
        if not isinstance(aic, Mapping):
            return None
        readback = aic.get("readback")
        if not isinstance(readback, Mapping) or readback.get("result") != "VERIFIED":
            return None
        observed = readback.get(family.observed_key)
        if not isinstance(observed, Mapping):
            return None
        return dict(observed)

    return project


class CorroboratedConfigReadback:
    """Two independent observations, or ``None``.

    Before a policy exists (``policy_id is None``, the prepare/pre-commit
    reading) the only source is the KPM stream, and that alone is enough for a
    baseline observation.  Once a policy is bound, the producer status must say
    ``VERIFIED`` **and** an independent fresh KPM re-read must agree with it;
    disagreement, an absent counter, or a terminal-without-verified status all
    yield ``None``.
    """

    def __init__(
        self,
        family: Campaign5Family,
        *,
        status_port: Any,
        kpm_reader: KpmConfigReader,
        monotonic_ms: Callable[[], int],
        sleep_ms: Callable[[int], None],
        cadence_ms: int,
        deadline_ms: int,
        projection: Optional[
            Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]
        ] = None,
    ) -> None:
        self._family = family
        self._status_port = status_port
        self._kpm = kpm_reader
        self._monotonic_ms = monotonic_ms
        self._sleep_ms = sleep_ms
        self._cadence_ms = cadence_ms
        self._deadline_ms = deadline_ms
        self._project = projection or make_status_projection(family)

    def _value_leaves(self, config: Mapping[str, Any]) -> Dict[str, Any]:
        return {field: config[field] for field in self._family.value_fields}

    def _on_axis(self, value_leaves: Mapping[str, Any]) -> Dict[str, Any]:
        # Return on the plan's contracted axis surface so the gateway's config
        # digest of the readback equals the plan's applied/baseline digest, the
        # same way steering returns ``{"servingCell": nci}``.
        return {self._family.axis: dict(value_leaves)}

    @staticmethod
    def _same_float(observed: Any, expected: Any) -> bool:
        """Compare an OAI ``float`` promoted to KPM ``double`` conservatively.

        Scheduler PF weight and TX attenuation are real-valued leaves in the
        Style-2 encoder.  A C ``float`` -> JSON/KPM ``double`` round-trip can
        differ by one float32 ULP, so exact Python equality would turn a valid
        independent readback into a false mismatch.  The tolerance is tight
        enough to reject a material configuration difference while covering
        that representational conversion.
        """
        if (isinstance(observed, bool) or isinstance(expected, bool)
                or not isinstance(observed, (int, float))
                or not isinstance(expected, (int, float))):
            return False
        return math.isfinite(observed) and math.isfinite(expected) and math.isclose(
            observed, expected, rel_tol=1e-7, abs_tol=1e-7
        )

    def _corroborates(
        self, fresh: Mapping[str, Any], producer_value: Mapping[str, Any]
    ) -> bool:
        """Require identical leaves, with float tolerance only where specified."""
        if set(fresh) != set(producer_value):
            return False
        float_fields = (
            set(self._family.value_fields)
            if self._family.key in {"priority", "power"}
            else set()
        )
        return all(
            self._same_float(fresh[field], producer_value[field])
            if field in float_fields else fresh[field] == producer_value[field]
            for field in producer_value
        )

    def __call__(
        self,
        *,
        scope: Mapping[str, Any],
        transaction_id: str,
        policy_id: Optional[str],
    ) -> Optional[Mapping[str, Any]]:
        del transaction_id
        counter = self._family.readback_counter
        started = self._monotonic_ms()
        if policy_id is None:
            # Pre-commit baseline read: the configuration counter is the only
            # source.  The live gNB publishes it once per KPM period, so a
            # single read taken between two indications sees nothing -- not
            # because the counter is absent but because no line has arrived
            # yet (observed over the air 2026-09-06: COUNTER_ABSENT for a UE
            # whose cap counter was on the stream one second later).  Poll at
            # the producer cadence until the deadline; a counter that never
            # arrives is still None, and the gateway still stages nothing.
            while True:
                fresh = self._kpm.read(counter, scope)
                if fresh is not None:
                    return self._on_axis(fresh)
                remaining = self._deadline_ms - (self._monotonic_ms() - started)
                if remaining <= 0:
                    return None
                self._sleep_ms(min(self._cadence_ms, remaining))
        while self._monotonic_ms() - started <= self._deadline_ms:
            status = self._status_port.get_policy_status(policy_id)
            projected = self._project(status)
            if projected is not None:
                producer_value = self._value_leaves(projected)
                fresh = self._kpm.read(counter, scope)
                if fresh is not None and self._corroborates(fresh, producer_value):
                    return self._on_axis(producer_value)
                # The producer says VERIFIED and the counter does not agree
                # yet -- either it has not been published for this identity, or
                # it is published and still carries the OLD value.  Wait for the
                # next indication in BOTH cases; past the deadline the answer is
                # still None.
                #
                # The second case used to return None on the spot.  That is
                # right for a scheduler parameter, which corroborates on the
                # first pass and never reaches here.  It is wrong for an RF
                # attenuation: measured over the air on 2026-09-18,
                # RAN.Cell.TxAttenuationDb took 10.5 s and 12.2 s to move after
                # the write (boards 20260918T104544 and T103405).  Both power
                # trials were called failures while the radio was seconds from
                # obeying -- KPM carried the requested 3.0 dB just after each
                # trial had already closed on PARTIAL_APPLY.
                remaining = self._deadline_ms - (self._monotonic_ms() - started)
                if remaining <= 0:
                    return None
                self._sleep_ms(min(self._cadence_ms, remaining))
                continue
            aic = status.get("aicStatus") if isinstance(status, Mapping) else None
            if isinstance(aic, Mapping) and aic.get("episodeTerminal") is True:
                return None
            remaining = self._deadline_ms - (self._monotonic_ms() - started)
            if remaining <= 0:
                return None
            self._sleep_ms(min(self._cadence_ms, remaining))
        return None
