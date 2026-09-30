"""The complete §9 AIC error catalogue and its fixed dispositions."""

from __future__ import annotations

from dataclasses import dataclass

ERROR_STAGES = (
    "ADMISSION", "DECISION", "CONTROL", "READBACK", "ROLLBACK", "RECOVERY", "TELEMETRY", "INTERNAL",
)


@dataclass(frozen=True)
class AICErrorSpec:
    code: str
    surface: str
    enforcement_episode: str
    retryable: bool
    write_may_have_occurred: bool


# Multiple contract rows for DEADLINE_EXCEEDED and APPLY_FAILED are retained as
# individual dispositions; callers must select the row matching their episode.
ERROR_SPECS = (
    AICErrorSpec("AIC_SCHEMA_INVALID", "PUT/POST 400", "no resource/episode", False, False),
    AICErrorSpec("AIC_RESOURCE_NOT_FOUND", "GET/PUT/DELETE 404", "no resource/episode", False, False),
    AICErrorSpec("AIC_UNSUPPORTED_OBJECTIVE", "PUT/POST 400", "no resource/episode", False, False),
    AICErrorSpec("AIC_VALIDITY_INVALID", "PUT/POST 400", "no resource/episode", False, False),
    AICErrorSpec("AIC_STALE_REVISION", "PUT 409", "existing resource unchanged", False, False),
    AICErrorSpec("AIC_IDEMPOTENCY_CONFLICT", "POST/PUT 409", "existing resource unchanged", False, False),
    AICErrorSpec("AIC_POLICY_CONFLICT", "POST/PUT 409", "existing resource unchanged", False, False),
    AICErrorSpec("AIC_SCOPE_NOT_FOUND", "accepted resource status", "NOT_ENFORCED + SCOPE_NOT_APPLICABLE; no episode", True, False),
    AICErrorSpec("AIC_CELL_NOT_ALLOWED", "accepted resource status", "NOT_ENFORCED + STATEMENT_NOT_APPLICABLE; no episode", False, False),
    AICErrorSpec("AIC_CELL_NOT_NEIGHBOR", "accepted resource status", "NOT_ENFORCED + STATEMENT_NOT_APPLICABLE; no episode", False, False),
    AICErrorSpec("AIC_CAPABILITY_MISMATCH", "accepted resource status", "NOT_ENFORCED + STATEMENT_NOT_APPLICABLE; no episode", False, False),
    AICErrorSpec("AIC_KPI_STALE", "status", "before: no episode; after: ABORTED_NO_WRITE", True, False),
    AICErrorSpec("AIC_KPI_MISSING", "status", "before: no episode; after: ABORTED_NO_WRITE", True, False),
    AICErrorSpec("AIC_E2_NOT_READY", "status", "before: no episode; after: ABORTED_NO_WRITE", True, False),
    AICErrorSpec("AIC_LOCK_CONFLICT", "status", "before: no episode; after: ABORTED_NO_WRITE", True, False),
    AICErrorSpec("AIC_ENVELOPE_VIOLATION", "status", "NOT_ENFORCED + ERROR; QUARANTINED", False, False),
    AICErrorSpec("AIC_DEADLINE_EXCEEDED", "status (before send)", "before: no episode; after: ABORTED_NO_WRITE", True, False),
    AICErrorSpec("AIC_DEADLINE_EXCEEDED", "status (after send)", "RECOVERY_PENDING", True, True),
    AICErrorSpec("AIC_CONTROL_TIMEOUT", "status", "NOT_ENFORCED + RECOVERY_PENDING; RECOVERY_PENDING", True, True),
    AICErrorSpec("AIC_APPLY_FAILED", "status (zero effect proven)", "terminal APPLY_FAILED; rollback forbidden", False, False),
    AICErrorSpec("AIC_APPLY_FAILED", "status (partial; rollback authorized)", "nonterminal APPLY_FAILED + rollback.REQUESTED", False, True),
    AICErrorSpec("AIC_APPLY_FAILED", "status (partial; no rollback)", "NOT_ENFORCED + RECOVERY_PENDING; fresh readback only", True, True),
    AICErrorSpec("AIC_READBACK_MISMATCH", "status", "ENFORCED + ACTIVE; READBACK_MISMATCH", False, True),
    AICErrorSpec("AIC_ROLLBACK_FAILED", "status", "NOT_ENFORCED + RECOVERY_PENDING then ERROR + QUARANTINED", False, True),
    AICErrorSpec("AIC_ROLLBACK_UNKNOWN", "status", "NOT_ENFORCED + RECOVERY_PENDING then ERROR + QUARANTINED", False, True),
    AICErrorSpec("AIC_RECOVERY_PENDING", "status", "NOT_ENFORCED + RECOVERY_PENDING; RECOVERY_PENDING", True, True),
    AICErrorSpec("AIC_INTERNAL_ERROR", "status or applicable 500", "fail closed; no terminal success", False, True),
)
ERROR_CODES = tuple(dict.fromkeys(spec.code for spec in ERROR_SPECS))
ERRORS_BY_CODE = {code: tuple(spec for spec in ERROR_SPECS if spec.code == code) for code in ERROR_CODES}
