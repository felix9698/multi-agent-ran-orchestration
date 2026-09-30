"""The advisory-only LLM transport seam.

Owner lane: **KAGT**.  New file (Gate 6, task section 11).

``docs/architecture/GATE1-MAP.md`` classifies ``decision/llm_backend.py`` as
``REUSE`` with a condition attached: "backend selection and transport
abstraction are not an actuator; all outputs must be constrained to the Kernel
mailbox."  This module is that constraint, made structural rather than
promised.

A strategy in this package never receives an ``LLMBackendBase``.  It receives
an :class:`AdvisoryTransport`, whose entire surface is *text in, text and a
token count out*.  There is no method that applies a configuration, opens a
trial, charges a ledger or reads a fencing token, and there is no attribute
through which the wrapped backend could be reached to grow one -- the backend
is held privately and only ``generate`` is ever called on it.  The old failure
this replaces is on record: GAP-01/GAP-02 in the Gate 1 map, where a
model-produced ``proposed_config`` became an actuator payload and a
model-produced ``confidence`` float was compared against a threshold that
changed admission and ledger state.

Two implementations ship here:

* :class:`ScriptedTransport` -- a hermetic mock.  Every test in this package
  runs against it, so the strategy suite makes no network call, needs no API
  key and is reproducible.  It records the exact prompts it was handed, which
  is how a test can prove the role-separated coordinator really did make three
  *different* calls rather than one call three times.
* :class:`LLMBackendTransport` -- the real adapter for
  ``decision/llm_backend.py``'s backends.  It does **not** import them.
  ``decision`` is on the assurance package's forbidden-import list (the seam
  test in ``tests/assurance/test_seams.py`` enforces it), so the dependency
  runs the other way: this class accepts anything with the backend's shape,
  and the composition root that builds a real one lives outside the package.
  That is the same arrangement ``assurance.live`` uses for its ports, and it
  is what keeps ``assurance`` importable with no provider SDK installed and
  no legacy module on the path.

Latency is measured here and handed to the meter, because this is the only
place that knows when a call started and stopped.  It is the sole clock read
in the strategy package; everything downstream of it is replayable arithmetic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Protocol, Sequence, Tuple, Union, runtime_checkable

from assurance.core.addressing import content_hash

__all__ = [
    "ADVISORY_TRANSPORT_SURFACE",
    "AdvisoryCompletion",
    "AdvisoryTransport",
    "LLMBackendTransport",
    "ScriptedTransport",
    "TransportError",
    "available_transport",
    "prompt_digest",
]

#: The complete public surface an :class:`AdvisoryTransport` may expose.  A
#: boundary test asserts every implementation's public names are a subset:
#: this is what "the adapter cannot reach an actuator or a ledger" means in
#: code rather than in prose.
ADVISORY_TRANSPORT_SURFACE: FrozenSet[str] = frozenset(
    {"complete", "describe", "transport_id", "model_identity"}
)


class TransportError(RuntimeError):
    """The transport could not produce a completion.

    Every provider failure -- unavailable backend, HTTP error, empty body,
    exception from an SDK -- arrives as this one type, so a strategy has a
    single thing to catch and a single reason string to record with the
    deterministic fallback that follows.
    """


@dataclass(frozen=True)
class AdvisoryCompletion:
    """One model answer, plus exactly the accounting §12 asks for.

    ``text`` is untrusted: it is a model's raw output, not yet parsed and
    never yet trusted.  Turning it into something typed is
    :mod:`assurance.advisors.strategies.schemas`' job, and whatever survives
    that is still inadmissible for a verdict, a bound or a charge.
    """

    text: str
    #: What the provider reported, verbatim.  ``None`` means it reported
    #: nothing, which is a different fact from zero and is kept distinct all
    #: the way to the meter: a transport that filled a missing count with 0
    #: would be pricing an unpriceable call on the provider's behalf, and the
    #: token ceiling would then be checked against a number this layer
    #: invented.  Reporting is this class's job; pricing is
    #: :class:`~assurance.advisors.strategies.budget.AgentBudgetMeter`'s, and
    #: it refuses what it cannot price.
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    latency_ms: float
    model_identity: str
    prompt_hash: str

    @property
    def total_tokens(self) -> Optional[int]:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return int(self.input_tokens) + int(self.output_tokens)


def prompt_digest(*, system_prompt: str, prompt: str) -> str:
    """Content hash of the exact pair of prompts that produced a completion.

    Uses the project's one canonicalizer
    (:func:`assurance.core.addressing.content_hash`) rather than a second
    hashing convention, so an advisory record's prompt hash is comparable with
    every other digest in the assurance record.
    """
    return content_hash({"systemPrompt": system_prompt or "", "prompt": prompt or ""})


def _reported_count(response: Any, name: str) -> Optional[int]:
    """One token count as the provider gave it, or ``None`` if it gave none.

    Deliberately not ``int(getattr(response, name, 0) or 0)``.  That idiom
    collapses three different situations -- "the provider said zero", "the
    provider said nothing" and "the attribute is absent" -- onto the single
    value the budget meter is cheapest to satisfy with.  A negative count is
    passed through unchanged rather than clamped, for the same reason: this
    layer reports what was said, and the meter decides what can be charged.
    """
    value = getattr(response, name, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@runtime_checkable
class AdvisoryTransport(Protocol):
    """Text in, text out.  Nothing else."""

    def complete(self, *, system_prompt: str, prompt: str) -> AdvisoryCompletion:
        """Return one completion, or raise :class:`TransportError`."""
        ...

    def describe(self) -> Mapping[str, Any]:
        """Transport provenance for the run record.  Never a credential."""
        ...


class ScriptedTransport:
    """A deterministic mock model, for hermetic tests and dry runs.

    *replies* is consumed one entry per :meth:`complete` call.  A ``str`` is
    returned as the model's text; a ``BaseException`` instance is raised, which
    is how a test scripts a provider failure without patching anything.
    Running past the end of the script raises :class:`TransportError` rather
    than looping, so a test that expects three calls and gets four finds out.

    Deliberately not seeded and not random.  The point of a scripted backend
    is that the *model's* contribution is held fixed while the strategy around
    it is what is being tested.
    """

    def __init__(
        self,
        replies: Sequence[Union[str, BaseException]],
        *,
        model_identity: str = "mock/scripted",
        transport_id: str = "transport-scripted",
        latency_ms: float = 1.0,
        input_tokens: Optional[int] = 100,
        output_tokens: Optional[int] = 40,
    ) -> None:
        self._replies: List[Union[str, BaseException]] = list(replies)
        self._model_identity = str(model_identity)
        self._transport_id = str(transport_id)
        self._latency_ms = float(latency_ms)
        # ``None`` and negative values are both scriptable on purpose: they
        # are what a misreporting provider looks like, and the meter's refusal
        # of them is the thing worth testing.
        self._input_tokens = None if input_tokens is None else int(input_tokens)
        self._output_tokens = None if output_tokens is None else int(output_tokens)
        self._prompts: List[Tuple[str, str]] = []
        self._calls = 0

    # -- transport surface -------------------------------------------------

    @property
    def transport_id(self) -> str:
        return self._transport_id

    @property
    def model_identity(self) -> str:
        return self._model_identity

    def complete(self, *, system_prompt: str, prompt: str) -> AdvisoryCompletion:
        self._prompts.append((system_prompt, prompt))
        if self._calls >= len(self._replies):
            self._calls += 1
            raise TransportError(
                f"scripted transport exhausted after {len(self._replies)} replies"
            )
        reply = self._replies[self._calls]
        self._calls += 1
        if isinstance(reply, BaseException):
            raise reply
        return AdvisoryCompletion(
            text=str(reply),
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            latency_ms=self._latency_ms,
            model_identity=self._model_identity,
            prompt_hash=prompt_digest(system_prompt=system_prompt, prompt=prompt),
        )

    def describe(self) -> Mapping[str, Any]:
        return {
            "transport": "scripted",
            "transportId": self._transport_id,
            "modelIdentity": self._model_identity,
            "scriptedReplies": len(self._replies),
        }

    # -- test-facing observation (not part of the transport surface) -------

    @property
    def prompts(self) -> Tuple[Tuple[str, str], ...]:
        """``(system_prompt, prompt)`` for every call, in order."""
        return tuple(self._prompts)

    @property
    def calls(self) -> int:
        return self._calls


class LLMBackendTransport:
    """Advisory-only adapter for a ``decision/llm_backend.py`` backend.

    Accepts any object with that module's ``LLMBackendBase`` shape --
    ``generate(prompt, system_prompt)`` returning a response with ``success``,
    ``content`` and token counts -- and checks the shape rather than the type,
    because ``assurance`` may not import ``decision`` (see this module's
    docstring).  Structural typing is not a loosening here: the point of the
    adapter is that the only thing it can do with a backend is ask it for
    text, and that is exactly what the check requires it to have.

    The wrapped backend is private and only ever asked to ``generate``.  What
    comes back is reduced to :class:`AdvisoryCompletion` -- text and counts --
    and everything else the provider returned (``parsed_json``, the reasoning
    fallback, the backend object itself) is dropped here rather than carried
    one layer further in.  The reasoning-content fallback in particular is
    named in the Gate 1 map as "an explicit untrusted-text input"; the answer
    is that this adapter has nowhere to put it.

    ``describe()`` is built from a whitelist, never from the backend's
    ``__dict__``.  Provider backends hold API keys as instance attributes, and
    a description assembled by reflection would put one in the run record.
    """

    def __init__(self, backend: Any, *, transport_id: str = "transport-llm-backend") -> None:
        if not callable(getattr(backend, "generate", None)) or not callable(
            getattr(backend, "is_available", None)
        ):
            raise TypeError(
                "LLMBackendTransport adapts a decision.llm_backend backend "
                "(generate + is_available + name); got "
                f"{type(backend).__name__}"
            )
        self._backend = backend
        self._transport_id = str(transport_id)
        self._model_identity = str(getattr(backend, "name", type(backend).__name__))

    # -- transport surface -------------------------------------------------

    @property
    def transport_id(self) -> str:
        return self._transport_id

    @property
    def model_identity(self) -> str:
        return self._model_identity

    def complete(self, *, system_prompt: str, prompt: str) -> AdvisoryCompletion:
        digest = prompt_digest(system_prompt=system_prompt, prompt=prompt)
        started = time.monotonic()
        try:
            response = self._backend.generate(prompt, system_prompt)
        except Exception as exc:  # noqa: BLE001 - every provider failure is one type here
            raise TransportError(f"{self._model_identity}: {exc!r}") from exc
        elapsed_ms = (time.monotonic() - started) * 1000.0

        if response is None or not getattr(response, "success", False):
            error = getattr(response, "error", None) if response is not None else "no response"
            raise TransportError(f"{self._model_identity}: {error}")
        text = getattr(response, "content", "") or ""
        if not text.strip():
            raise TransportError(f"{self._model_identity}: empty completion")

        # The backend reports its own latency when it has one; otherwise the
        # wall-clock measured here stands in.  Either way the number handed to
        # the meter is a real measurement, never a default.
        reported = float(getattr(response, "latency_ms", 0.0) or 0.0)
        return AdvisoryCompletion(
            text=text,
            input_tokens=_reported_count(response, "input_tokens"),
            output_tokens=_reported_count(response, "output_tokens"),
            latency_ms=reported if reported > 0.0 else elapsed_ms,
            model_identity=str(getattr(response, "model", "") or self._model_identity),
            prompt_hash=digest,
        )

    def describe(self) -> Mapping[str, Any]:
        described: Dict[str, Any] = {
            "transport": "decision.llm_backend",
            "transportId": self._transport_id,
            "modelIdentity": self._model_identity,
        }
        return described


def available_transport(backend: Any) -> Optional[LLMBackendTransport]:
    """Wrap *backend* only when it reports itself usable, else ``None``.

    A convenience for the conditional smoke path: a run that has no API key
    configured gets ``None`` and skips, rather than building a transport that
    fails on its first call and reports the miss as a model failure.
    """
    try:
        transport = LLMBackendTransport(backend)
    except TypeError:
        return None
    if not bool(getattr(backend, "is_available", lambda: False)()):
        return None
    return transport
