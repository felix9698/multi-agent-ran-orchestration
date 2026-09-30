"""The one shape every strategy's ``describe()`` returns.

Owner lane: **KAGT**.  New file (Gate 6, task section 11).

:meth:`~assurance.advisors.strategy.AdvisoryStrategy.describe` exists so the
run record can state, for every strategy, "model identity, prompt version,
token/tool-call budget, seed, temperature".  Task section 11 makes one of
those load-bearing: the monolithic LLM comparison "uses the same foundation
model and a controlled equivalent token/tool-call budget".  A claim of
equivalent budgets is only checkable if both strategies report their budget in
the same fields -- if the proposed method reported ``tokenBudget`` and the
monolithic one reported ``maxTokens``, the comparison would be an assertion
rather than a measurement.

So the key set is fixed here, once, and every strategy in this package builds
its description through :func:`strategy_description`.  A strategy that has no
model, no prompt and no seed reports ``None`` for those fields rather than
omitting them: a missing key and a deliberate "not applicable" are different
statements, and only one of them survives being written into a CSV column.

Nothing here may carry a credential.  ``modelIdentity`` is a model *name*
("claude-sonnet-4-6", "mock/scripted"), never an endpoint with a key in it.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Mapping, Optional

__all__ = [
    "CREDENTIAL_KEY_MARKERS",
    "CREDENTIAL_KEY_NAMES",
    "STRATEGY_DESCRIPTION_KEYS",
    "is_credential_key",
    "strategy_description",
]

#: Exactly the keys every ``describe()`` in this package returns.
STRATEGY_DESCRIPTION_KEYS: FrozenSet[str] = frozenset(
    {
        "strategyId",
        "strategyKind",
        "modelIdentity",
        "promptVersion",
        "tokenBudget",
        "toolCallBudget",
        "latencyBudgetMs",
        "seed",
        "temperature",
    }
)

#: Substrings that must never appear in a description key, matched after the
#: key is reduced to letters and digits.  The constraint list forbids putting
#: "actual credential, password, token or private key" in source, evidence,
#: log or export, and a run record built from ``describe()`` is an export.
#:
#: Bare ``token`` is deliberately *not* a substring marker -- ``tokenBudget``
#: is one of the nine frozen fields and is a count, not a secret.  It is an
#: exact-name marker instead, in :data:`CREDENTIAL_KEY_NAMES`.
CREDENTIAL_KEY_MARKERS: FrozenSet[str] = frozenset(
    {
        "secret",
        "password",
        "credential",
        "apikey",
        "privatekey",
        "accesstoken",
        "authtoken",
        "sessiontoken",
        "bearer",
        "authorization",
    }
)

#: Key names that are a credential when they stand alone, whatever they would
#: be as a substring.
CREDENTIAL_KEY_NAMES: FrozenSet[str] = frozenset({"key", "token", "auth"})


def is_credential_key(name: str) -> bool:
    """True when *name* is a place a credential could be hiding.

    One predicate, used both by :func:`strategy_description` when it merges
    *extra* and by the boundary test that scans a whole description.
    """
    normalised = "".join(character for character in str(name).lower() if character.isalnum())
    if normalised in CREDENTIAL_KEY_NAMES:
        return True
    return any(marker in normalised for marker in CREDENTIAL_KEY_MARKERS)


def strategy_description(
    *,
    strategy_id: str,
    strategy_kind: Any,
    model_identity: Optional[str] = None,
    prompt_version: Optional[str] = None,
    token_budget: int = 0,
    tool_call_budget: int = 0,
    latency_budget_ms: float = 0.0,
    seed: Optional[int] = None,
    temperature: Optional[float] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build one strategy description with the frozen key set.

    *extra* is for strategy-specific provenance that is genuinely not one of
    the nine fields -- the adversarial strategy's move order, for instance.
    It is merged in, and it is checked against
    :data:`CREDENTIAL_KEY_MARKERS` on the way, so the escape hatch cannot
    become the place a key ends up.
    """
    described: Dict[str, Any] = {
        "strategyId": str(strategy_id),
        "strategyKind": getattr(strategy_kind, "value", strategy_kind),
        "modelIdentity": model_identity,
        "promptVersion": prompt_version,
        "tokenBudget": int(token_budget),
        "toolCallBudget": int(tool_call_budget),
        "latencyBudgetMs": float(latency_budget_ms),
        "seed": seed,
        "temperature": temperature,
    }
    for key, value in dict(extra or {}).items():
        if str(key) in STRATEGY_DESCRIPTION_KEYS:
            raise ValueError(
                f"describe() may not shadow the frozen field {key!r} through extra; "
                "the nine fields are what two strategies' budgets are compared on"
            )
        if is_credential_key(key):
            raise ValueError(
                f"describe() may not carry {key!r}: a strategy description is "
                "written into the run record, and the run record carries no credential"
            )
        described[str(key)] = value
    return described
