"""Contract-defined identifiers, UTC timestamps, priorities and ownership."""

from __future__ import annotations

import re
from dataclasses import dataclass

UTC_DATETIME_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:/-]+$"
PRODUCER_ID_PATTERN = r"^[A-Za-z0-9._:/-]+$"
IDENTIFIER_MIN_LENGTH = 1
IDENTIFIER_MAX_LENGTH = 128
UTC_DATETIME_RE = re.compile(UTC_DATETIME_PATTERN)
IDEMPOTENCY_KEY_RE = re.compile(IDEMPOTENCY_KEY_PATTERN)
PRODUCER_ID_RE = re.compile(PRODUCER_ID_PATTERN)
PRIORITY_VALUES = {"CRITICAL": 100, "HIGH": 75, "MEDIUM": 50, "LOW": 25}


@dataclass(frozen=True)
class IdentifierOwner:
    identifier: str
    owner: str
    rule: str


IDENTIFIER_OWNERS = (
    IdentifierOwner("policyTypeId", "contract", "AIC_UECellSteering_1.0.0"),
    IdentifierOwner("nearRtRicId", "backend deployment/release", "pinned in backend capability manifest"),
    IdentifierOwner("policyId", "Non-RT RIC A1-P Consumer", "stable for the policy resource"),
    IdentifierOwner("intentId", "rApp", "stable for operator intent"),
    IdentifierOwner("intentRevision", "rApp", "increments on intent reassessment"),
    IdentifierOwner("policyRevision", "rApp", "monotonic; sole authority"),
    IdentifierOwner("idempotencyKey", "rApp", "stable for logical revision retry"),
    IdentifierOwner("episodeId", "Near-RT/xApp", "one per decision episode"),
    IdentifierOwner("transactionId", "Near-RT/xApp", "one per E2 control attempt"),
    IdentifierOwner("actionId", "Near-RT/xApp", "one per concrete action"),
    IdentifierOwner("correlationId", "rApp", "stable end-to-end trace"),
)


def priority_value(priority: str) -> int:
    return PRIORITY_VALUES[priority]
