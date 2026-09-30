"""The shared token / tool-call / latency meter both LLM strategies spend from.

Owner lane: **KAGT**.  New file (Gate 6, task sections 11 and 12).

Task section 11 states the comparison condition that this module exists to
make true rather than claimed:

    monolithic LLM은 proposed method와 같은 foundation model과 **통제된 동등
    token/tool-call budget**을 사용한다.

An equivalent budget is not a number written into two docstrings.  It is one
meter class, one :class:`AgentBudget` value object, and one enforcement rule,
so the role-separated coordinator's three calls and the monolithic strategy's
one call are stopped by exactly the same ceiling.  If each strategy counted
its own spend its own way, the paper would be comparing accounting systems.

Task section 12 then requires the run record to carry "agent token/tool-call/
latency" per run and per aggregate.  The same meter produces that: every call
is recorded as an :class:`AgentCallRecord` carrying the role that made it, its
token split, its tool-call count and its wall-clock latency, and the totals
are readable without re-deriving them from a log.

Two scopes, deliberately:

* the **proposal window** -- one ``propose()`` call -- is what the ceiling is
  enforced against, because that is the unit a strategy is asked to do work
  in, and a per-run ceiling would let a strategy spend a case's entire budget
  answering its first question;
* the **cumulative** totals span the meter's whole life and are what the run
  record reports.

Latency is a budget dimension, not just a statistic.  Design section 8
requires every case to terminate finitely, and
:class:`~assurance.advisors.coordinator.StrategyBackedEvidenceCoordinator`
already bounds a strategy from outside with a wall-clock timeout; the latency
ceiling here is the strategy bounding *itself*, so a model that answers slowly
three times in a row spends its deadline in a recorded, attributable way
instead of being killed anonymously from outside.

Pure stdlib, no clock read of its own: the caller passes the measured latency
in.  A meter that timed calls itself could not be replayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "DEFAULT_COMPARABLE_BUDGET",
    "AgentBudget",
    "AgentBudgetMeter",
    "AgentCallRecord",
    "BudgetDimension",
    "BudgetExceeded",
    "NEGATIVE",
    "OVER_LIMIT",
    "UNMETERED",
]

#: The three metered dimensions, spelled the way the run record spells them.
BudgetDimension = str

_TOKENS: BudgetDimension = "tokens"
_TOOL_CALLS: BudgetDimension = "toolCalls"
_LATENCY: BudgetDimension = "latencyMs"


#: Why a call could not be afforded.  ``over-limit`` is the ordinary case;
#: the other two are calls the meter could not price at all and therefore
#: refuses to let past -- see :meth:`AgentBudgetMeter.charge`.
OVER_LIMIT = "over-limit"
UNMETERED = "unmetered"
NEGATIVE = "negative"


class BudgetExceeded(RuntimeError):
    """A metered call crossed a ceiling, or could not be priced at all.

    Carries the dimension, the spend, the limit and the reason, so the record
    left with the resulting deterministic fallback names what actually
    happened -- task section 12 counts "fallback usage", and a fallback whose
    reason is just "budget" cannot be attributed to tokens rather than
    latency, or to an over-spend rather than an unpriceable call.

    One exception type for all three reasons, deliberately.  Every caller's
    correct response is identical -- stop, record, fall back deterministically
    -- and a second exception class would have to be added to every existing
    handler for that to stay true, which is precisely how a fail-closed path
    becomes a conditional one.
    """

    def __init__(
        self,
        dimension: BudgetDimension,
        used: float,
        limit: float,
        *,
        reason: str = OVER_LIMIT,
    ) -> None:
        super().__init__(f"{dimension} budget {reason}: {used} of {limit}")
        self.dimension = dimension
        self.used = used
        self.limit = limit
        self.reason = reason


@dataclass(frozen=True)
class AgentBudget:
    """One controlled ceiling, shared by the strategies being compared.

    Attributes
    ----------
    max_total_tokens:
        Input plus output tokens per proposal window.  Counted together
        because a strategy can trade one for the other -- a prompt that
        pre-computes what the model would otherwise have written out is
        cheaper in output tokens and no cheaper overall.
    max_tool_calls:
        Model invocations per proposal window.  The role-separated
        coordinator spends three; the monolithic strategy spends one.  The
        *ceiling* is identical, which is the controlled part; how many of it
        a strategy uses is the thing being measured.
    max_latency_ms:
        Summed call latency per proposal window.
    """

    max_total_tokens: int
    max_tool_calls: int
    max_latency_ms: float

    def __post_init__(self) -> None:
        if self.max_total_tokens <= 0:
            raise ValueError("max_total_tokens must be positive")
        if self.max_tool_calls <= 0:
            raise ValueError("max_tool_calls must be positive")
        if self.max_latency_ms <= 0:
            raise ValueError("max_latency_ms must be positive")

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "maxTotalTokens": int(self.max_total_tokens),
            "maxToolCalls": int(self.max_tool_calls),
            "maxLatencyMs": float(self.max_latency_ms),
        }


#: The budget a comparison run gives to *both* LLM strategies unless the batch
#: plan overrides it.  Three roles at roughly 1.3k tokens each, with headroom:
#: large enough that the role-separated method is not budget-starved relative
#: to the monolithic one, which would make the comparison a rigged race.
DEFAULT_COMPARABLE_BUDGET = AgentBudget(
    max_total_tokens=4096, max_tool_calls=4, max_latency_ms=20_000.0
)


@dataclass(frozen=True)
class AgentCallRecord:
    """One metered model call, in the run record's own vocabulary."""

    #: Which advisory role made the call.  Spelled ``agent_role`` and never
    #: ``role``: the seam test bans the bare identifier across this package
    #: (design section 5 removes human identity and role from the record), and
    #: an advisory role is a different thing wearing the same word.
    agent_role: str
    model_identity: str
    prompt_hash: str
    #: What the provider reported.  ``None`` means it reported *nothing* --
    #: which is a different fact from "zero tokens", and is why these are
    #: optional rather than defaulted.  A meter that read a missing count as
    #: zero would price an unpriceable call at nothing and let a run spend
    #: past its ceiling without any dimension ever crossing it.
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    latency_ms: float
    tool_calls: int
    correlation_id: str

    @property
    def total_tokens(self) -> Optional[int]:
        """Reported spend, or ``None`` when the provider did not report."""
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return int(self.input_tokens) + int(self.output_tokens)

    @property
    def is_priceable(self) -> bool:
        """True when this call can be charged against a ceiling.

        False for a missing count and for a negative one.  A negative count
        is worse than a missing one: added to a running total it *reduces*
        it, so a provider reporting ``-10_000`` would buy back an entire
        window's allowance.
        """
        if self.input_tokens is None or self.output_tokens is None:
            return False
        if self.input_tokens < 0 or self.output_tokens < 0:
            return False
        return self.latency_ms >= 0.0 and self.tool_calls >= 0

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "agentRole": self.agent_role,
            "modelIdentity": self.model_identity,
            "promptHash": self.prompt_hash,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.total_tokens,
            "priceable": self.is_priceable,
            "latencyMs": float(self.latency_ms),
            "toolCalls": int(self.tool_calls),
            "correlationId": self.correlation_id,
        }


def _pricing_fault(record: AgentCallRecord) -> Tuple[BudgetDimension, Any, str]:
    """Which quantity made *record* unpriceable, and why.

    Named separately so the refusal reports the dimension that was actually
    wrong.  A negative latency raised as a *token* violation would send
    whoever reads the run record looking in the wrong column.
    """
    if record.input_tokens is None or record.output_tokens is None:
        return _TOKENS, None, UNMETERED
    if record.input_tokens < 0 or record.output_tokens < 0:
        return _TOKENS, record.total_tokens, NEGATIVE
    if record.latency_ms < 0.0:
        return _LATENCY, record.latency_ms, NEGATIVE
    return _TOOL_CALLS, record.tool_calls, NEGATIVE


@dataclass
class _Window:
    """Spend inside one ``propose()`` call."""

    correlation_id: str = ""
    tokens: int = 0
    tool_calls: int = 0
    latency_ms: float = 0.0
    #: Latched the moment a call arrives that the meter cannot price.  Once
    #: set, nothing further can be afforded in this window: an unpriced call
    #: means the running total is no longer a true statement about spend, and
    #: a ceiling checked against an untrue total is not a ceiling.
    unmetered: bool = False
    records: List[AgentCallRecord] = field(default_factory=list)


class AgentBudgetMeter:
    """Meters and enforces one :class:`AgentBudget`.

    Not thread-safe and deliberately not shared between strategies: two
    strategies being compared get *equal* budgets, never the *same* meter, or
    the first one to run would starve the second.  :meth:`sibling` is the
    supported way to make the second meter, so "equal" is enforced by
    construction rather than by copying a literal.
    """

    def __init__(self, budget: AgentBudget, *, meter_id: str = "agent-budget-meter") -> None:
        if not isinstance(budget, AgentBudget):
            raise TypeError("budget must be an AgentBudget")
        self._budget = budget
        self._meter_id = str(meter_id)
        self._window = _Window()
        self._records: List[AgentCallRecord] = []
        self._cumulative_tokens = 0
        self._cumulative_tool_calls = 0
        self._cumulative_latency_ms = 0.0
        self._windows_opened = 0
        self._unpriceable_calls = 0

    # -- identity ---------------------------------------------------------

    @property
    def budget(self) -> AgentBudget:
        return self._budget

    @property
    def meter_id(self) -> str:
        return self._meter_id

    def sibling(self, *, meter_id: str) -> "AgentBudgetMeter":
        """A second, independent meter on the identical budget.

        The one supported way to give a comparison's other arm its budget.
        """
        return AgentBudgetMeter(self._budget, meter_id=meter_id)

    # -- the proposal window ----------------------------------------------

    def begin_proposal(self, correlation_id: str) -> None:
        """Open a fresh enforcement window for one ``propose()`` call."""
        self._window = _Window(correlation_id=str(correlation_id))
        self._windows_opened += 1

    def ensure_room(self, *, tool_calls: int = 1) -> None:
        """Refuse a call that cannot be afforded before it is made.

        Raises :class:`BudgetExceeded` when the window has already spent its
        tool-call allowance, its tokens or its latency, or when an earlier
        call in this window could not be priced.  Checked *before* the
        transport is touched, so a strategy out of budget makes no model call
        at all rather than making one it cannot pay for.
        """
        if self._window.unmetered:
            raise BudgetExceeded(
                _TOKENS,
                self._window.tokens,
                self._budget.max_total_tokens,
                reason=UNMETERED,
            )
        if self._window.tool_calls + int(tool_calls) > self._budget.max_tool_calls:
            raise BudgetExceeded(
                _TOOL_CALLS, self._window.tool_calls + int(tool_calls), self._budget.max_tool_calls
            )
        if self._window.tokens >= self._budget.max_total_tokens:
            raise BudgetExceeded(_TOKENS, self._window.tokens, self._budget.max_total_tokens)
        if self._window.latency_ms >= self._budget.max_latency_ms:
            raise BudgetExceeded(_LATENCY, self._window.latency_ms, self._budget.max_latency_ms)

    def charge(
        self,
        *,
        agent_role: str,
        model_identity: str,
        prompt_hash: str,
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        latency_ms: float,
        tool_calls: int = 1,
    ) -> AgentCallRecord:
        """Record one completed call, then enforce the ceiling.

        The record is appended **before** anything is checked, and it is
        appended even when the call is the one that overspends and even when
        it cannot be priced.  Tokens already emitted by a model cannot be
        un-spent, and a meter that dropped such a call would under-report
        exactly the runs the paper most needs to see.  What the record says is
        what the provider claimed; whether that claim can be *charged* is a
        separate judgement, made next.

        A call the meter cannot price -- a missing token count, or a negative
        one -- latches the window unmetered and raises.  Neither is treated as
        zero.  A missing count read as zero would let a provider that reports
        no usage run without limit; a negative count added to a running total
        would *refund* allowance, so a single ``-10_000`` would buy back a
        whole window.  Both are ways to leave the ceiling nominally uncrossed
        while the spend behind it is unknown, so both fail closed here, in the
        one place both LLM arms share (task section 11's equal budgets).
        """
        record = AgentCallRecord(
            agent_role=str(agent_role),
            model_identity=str(model_identity),
            prompt_hash=str(prompt_hash),
            input_tokens=None if input_tokens is None else int(input_tokens),
            output_tokens=None if output_tokens is None else int(output_tokens),
            latency_ms=float(latency_ms),
            tool_calls=int(tool_calls),
            correlation_id=self._window.correlation_id,
        )
        self._window.records.append(record)
        self._records.append(record)

        if not record.is_priceable:
            self._window.unmetered = True
            self._unpriceable_calls += 1
            # The tool call itself is real and is still counted; only the
            # quantities the meter could not trust are left uncharged.
            self._window.tool_calls += max(record.tool_calls, 0)
            self._cumulative_tool_calls += max(record.tool_calls, 0)
            dimension, used, reason = _pricing_fault(record)
            raise BudgetExceeded(
                dimension, used, self._limit_for(dimension), reason=reason
            )

        self._window.tokens += record.total_tokens
        self._window.tool_calls += record.tool_calls
        self._window.latency_ms += record.latency_ms
        self._cumulative_tokens += record.total_tokens
        self._cumulative_tool_calls += record.tool_calls
        self._cumulative_latency_ms += record.latency_ms

        if self._window.tokens > self._budget.max_total_tokens:
            raise BudgetExceeded(_TOKENS, self._window.tokens, self._budget.max_total_tokens)
        if self._window.tool_calls > self._budget.max_tool_calls:
            raise BudgetExceeded(
                _TOOL_CALLS, self._window.tool_calls, self._budget.max_tool_calls
            )
        if self._window.latency_ms > self._budget.max_latency_ms:
            raise BudgetExceeded(_LATENCY, self._window.latency_ms, self._budget.max_latency_ms)
        return record

    # -- read-only reporting ----------------------------------------------

    @property
    def records(self) -> Tuple[AgentCallRecord, ...]:
        """Every metered call over the meter's whole life, in call order."""
        return tuple(self._records)

    def window_totals(self) -> Mapping[str, Any]:
        return {
            "correlationId": self._window.correlation_id,
            "tokens": self._window.tokens,
            "toolCalls": self._window.tool_calls,
            "latencyMs": self._window.latency_ms,
            "calls": len(self._window.records),
            "unmetered": self._window.unmetered,
        }

    def cumulative_totals(self) -> Mapping[str, Any]:
        """The §12 line items: tokens, tool calls, latency, and call count."""
        return {
            "meterId": self._meter_id,
            "budget": self._budget.to_canonical_dict(),
            "tokens": self._cumulative_tokens,
            "toolCalls": self._cumulative_tool_calls,
            "latencyMs": self._cumulative_latency_ms,
            "calls": len(self._records),
            "proposalWindows": self._windows_opened,
            # A run whose token total looks small because some calls could not
            # be priced is not the same run as one that genuinely spent little.
            "unpriceableCalls": self._unpriceable_calls,
        }

    def _limit_for(self, dimension: BudgetDimension) -> float:
        if dimension == _TOOL_CALLS:
            return float(self._budget.max_tool_calls)
        if dimension == _LATENCY:
            return self._budget.max_latency_ms
        return float(self._budget.max_total_tokens)

    def remaining(self) -> Mapping[str, Any]:
        """What is left in the *current* window.

        All zero once the window is unmetered: with an unpriced call in it,
        the running total is no longer a true statement about spend, and
        reporting headroom against an untrue total would be worse than
        reporting none.
        """
        if self._window.unmetered:
            return {"tokens": 0, "toolCalls": 0, "latencyMs": 0.0}
        return {
            "tokens": max(0, self._budget.max_total_tokens - self._window.tokens),
            "toolCalls": max(0, self._budget.max_tool_calls - self._window.tool_calls),
            "latencyMs": max(0.0, self._budget.max_latency_ms - self._window.latency_ms),
        }

