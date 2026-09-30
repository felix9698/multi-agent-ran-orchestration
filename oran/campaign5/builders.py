"""Per-family policy builders: a gateway command becomes a validated A1 body.

This is the only place a Campaign 5 policy body is constructed.  ``R1Adapter``
refuses to be built without a ``policy_builder`` (*"an R1 adapter without a
validated policy builder cannot act"*), and the builder is where the closed
gateway command vocabulary -- ``scope``/``axis``/``value`` plus the Kernel
token's ``transactionId``/``fencingToken`` -- turns into the schema-validated
``{config, validity, trace}`` object the A1-P producer will admit.

The builder is a *factory* closure, exactly like the steering
``policy_builder_factory`` bound after epoch freeze in
``tools/g3ota/composition.py``: the validity window is not in the command (a
command carries what to do, not how long a policy may live), so it is injected
here from the permit lease.  Nothing behaviour-bearing is caller free-text.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, Mapping, Optional

from .families import Campaign5Error, Campaign5Family, validate_campaign5

__all__ = ["Campaign5BuilderError", "make_policy_builder"]


class Campaign5BuilderError(ValueError):
    """A gateway command cannot be turned into a valid Campaign 5 policy body."""


ValidityProvider = Callable[[Mapping[str, Any]], Mapping[str, str]]


def _coerce_value(family: Campaign5Family, value: Any) -> Dict[str, Any]:
    """Return the family's value leaves as a mapping.

    A single-leaf family (cap/priority/power) may carry a bare scalar on the
    plan step, mirroring steering's ``servingCell`` string; a multi-leaf family
    (mcs) carries a mapping of its leaves.  Either way the result is the mapping
    ``{value_field: value}``.
    """
    if len(family.value_fields) == 1 and not isinstance(value, Mapping):
        return {family.value_fields[0]: value}
    if not isinstance(value, Mapping):
        raise Campaign5BuilderError(
            f"{family.key}: value must be a mapping of {family.value_fields}"
        )
    missing = [field for field in family.value_fields if field not in value]
    extra = [field for field in value if field not in family.value_fields]
    if missing or extra:
        raise Campaign5BuilderError(
            f"{family.key}: value must be exactly {family.value_fields}, "
            f"missing={missing} unexpected={extra}"
        )
    return {field: value[field] for field in family.value_fields}


def make_policy_builder(
    family: Campaign5Family,
    *,
    validity_provider: ValidityProvider,
) -> Callable[[Mapping[str, Any]], Dict[str, Any]]:
    """Return a durable-revision-seeded policy builder for *family*.

    ``last_fencing_token`` is the ``trace.fencingToken`` the producer currently
    holds for the policy about to be adopted.  It is *not* the Kernel's fence:
    the Kernel's is per-case and restarts, the producer's only rises.  Omitted,
    the Kernel's fence is sent verbatim, which is right for every write that is
    not a cross-case takeover.

    ``last_revision`` is the last A1 revision persisted by the binding journal,
    or zero before the scope has ever owned a policy.  The journal, not this
    closure, is the restart-safe revision authority.  The closure retains only
    the latest draft per transaction so an in-process same-fence replay can
    return the byte-identical body and a lower fence can fail closed.  Omitting
    the keyword is a compatibility path for adapter callers predating the
    journal contract; it cannot carry revision state across reconstruction.
    """

    # transaction -> (fence, behaviour-bearing config, exact body).  This cache
    # is replay/staleness metadata only; revision allocation always starts from
    # the journal-provided ``last_revision`` and therefore survives rebuilding
    # this factory.
    drafts: Dict[str, tuple[int, Dict[str, Any], Dict[str, Any]]] = {}

    def build(command: Mapping[str, Any], *,
              last_revision: Optional[int] = None,
              last_fencing_token: Optional[int] = None) -> Dict[str, Any]:
        if not isinstance(command, Mapping):
            raise Campaign5BuilderError("command must be a mapping")
        if (last_revision is not None
                and (isinstance(last_revision, bool)
                     or not isinstance(last_revision, int)
                     or last_revision < 0)):
            raise Campaign5BuilderError(
                "last_revision must be a non-negative integer"
            )
        if (last_fencing_token is not None
                and (isinstance(last_fencing_token, bool)
                     or not isinstance(last_fencing_token, int)
                     or last_fencing_token < 0)):
            raise Campaign5BuilderError(
                "last_fencing_token must be a non-negative integer"
            )
        scope = command.get("scope")
        if not isinstance(scope, Mapping):
            raise Campaign5BuilderError("command scope must be a mapping")
        axis = command.get("axis")
        if axis is not None and axis != family.axis:
            raise Campaign5BuilderError(
                f"{family.key}: command axis {axis!r} is not {family.axis!r}"
            )
        # Identity comes only from the scope; the value only from the step.
        config: Dict[str, Any] = {}
        for field in family.scope_fields:
            if field not in scope:
                raise Campaign5BuilderError(
                    f"{family.key}: scope is missing identity leaf {field!r}"
                )
            config[field] = scope[field]
        config.update(_coerce_value(family, command.get("value")))

        # Cross-field invariants the JSON Schema cannot express.
        if family.key == "mcs" and config["minDlMcs"] > config["maxDlMcs"]:
            raise Campaign5BuilderError(
                "dl-mcs-bounds requires minDlMcs <= maxDlMcs"
            )

        transaction_id = command.get("transactionId")
        fencing_token = command.get("fencingToken")
        if not isinstance(transaction_id, str) or not transaction_id:
            raise Campaign5BuilderError("command lacks a transactionId")
        if (isinstance(fencing_token, bool)
                or not isinstance(fencing_token, int)
                or fencing_token < 0):
            raise Campaign5BuilderError(
                "command lacks a non-negative fencingToken"
            )

        current = drafts.get(transaction_id)
        if current is not None:
            latest_fence, latest_config, latest_body = current
            if fencing_token < latest_fence:
                raise Campaign5BuilderError(
                    "command carries a stale fencingToken"
                )
            if fencing_token == latest_fence:
                if config != latest_config:
                    raise Campaign5BuilderError(
                        "same fencingToken cannot change the policy body"
                    )
                # PREPARE and COMMIT can draft the same policy.  Return the
                # first validated bytes rather than re-running a validity clock.
                return copy.deepcopy(latest_body)
            latest_revision = latest_body["trace"]["revision"]
            if last_revision is not None and last_revision < latest_revision:
                raise Campaign5BuilderError(
                    "last_revision is older than the latest drafted revision"
                )

        # Compatibility for callers on the adapter side of the coordinated
        # change: until they pass the keyword, an in-process higher-fence draft
        # continues from this builder's last validated body.  Once supplied,
        # the durable seed always wins; reconstruction therefore never depends
        # on this fallback.
        if last_revision is None:
            last_revision = (
                current[2]["trace"]["revision"] if current is not None else 0
            )

        # A1 revision and the Kernel fence are deliberately separate axes.  The
        # durable journal supplies the former's seed; the latter is copied below
        # byte-for-byte, including its valid first value zero.
        revision = last_revision + 1

        # The Kernel's fence is per-case and legitimately restarts at zero; the
        # producer's is per-policy and only ever rises.  Adopting a policy a
        # *previous case* left live therefore has to clear the fence that case
        # reached, not this case's.  ``last_fencing_token`` is what the producer
        # says it holds right now, so one above it is the smallest value it will
        # accept, and the Kernel's own number still wins whenever it is ahead.
        sent_fence = fencing_token
        if last_fencing_token is not None:
            sent_fence = max(sent_fence, last_fencing_token + 1)

        validity = validity_provider(command)
        body = {
            "config": config,
            "validity": {
                "notBefore": validity["notBefore"],
                "notAfter": validity["notAfter"],
            },
            "trace": {
                # A1 revision starts at one from the durable seed.  The Kernel
                # fence is copied verbatim, including its valid first value
                # zero; the draft cache rejects an older fence while preserving
                # retry idempotency at an equal one.
                # 2026-09-20: the producer derives the A1 policy id from
                # (nearRtRicId, policyTypeId, traceId).  With the bare
                # transaction id, two UEs (or two cells) written with one type in
                # one trial got the SAME id, and the second cell's CREATE was
                # refused "policy id cannot move to another cell worker/ledger"
                # (board 2026-09-20 03:30, pfWeight@ue1 + pfWeight@ue3).  The
                # scope identity makes it one id per (type, UE/cell); a retry of
                # the same write keeps its id, so creation stays idempotent.
                "traceId": trace_id_for(transaction_id, config, family.scope_fields),
                "revision": revision,
                "fencingToken": sent_fence,
            },
        }
        try:
            validate_campaign5(body, f"{family.policy_type_id}.policy")
        except Campaign5Error as exc:
            raise Campaign5BuilderError(str(exc)) from exc
        drafts[transaction_id] = (
            fencing_token, copy.deepcopy(config), copy.deepcopy(body)
        )
        return copy.deepcopy(body)

    return build


def trace_id_for(transaction_id: str, config: Mapping[str, Any],
                 scope_fields: Any) -> str:
    """``traceId`` for one policy write: the transaction plus the scope it names."""
    scope = "/".join(f"{field}={config[field]}" for field in scope_fields)
    return f"{transaction_id}#{scope}" if scope else transaction_id


def fixed_validity(not_before: str, not_after: str) -> ValidityProvider:
    """A constant validity window, for tests and for a deployment that pins one."""

    def provider(command: Mapping[str, Any]) -> Mapping[str, str]:
        del command
        return {"notBefore": not_before, "notAfter": not_after}

    return provider
