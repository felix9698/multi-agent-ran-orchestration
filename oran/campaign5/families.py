"""The four Campaign 5 action families and their frozen A1 policy types.

Schemas live in ``contracts/oran-aic/campaign5/`` -- deliberately outside the
byte-pinned ``shared-contract-bundle`` (whose ``aic.ran-capability`` schema
hard-codes ``policyTypes`` as a one-element ``const`` and ``schemaDigests`` as a
single closed pair).  So this module follows the ``AIC_SliceSLATarget_1.0.0``
precedent (``oran/slice_actuator/a1.py``): it loads and validates each schema
with its own offline ``Draft202012Validator``, never through the frozen
``ContractValidator`` bundle.

Because the producer is in-repo (design section 4.6 Option B), this module owns
all three digests the discovery gate cross-checks -- the R1-advertised
``schemaSha256``, the capability manifest's ``schemaDigests``, and this tree's
pinned ``jcs_sha256(load_schema(...))`` -- and :func:`verify_campaign5_discovery`
refuses unless they agree, exactly as the steering gate does in
``tools/g3ota/composition.py``.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from oran.contract.jcs import jcs_sha256

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_DIR = _ROOT / "contracts" / "oran-aic" / "campaign5"

CAPABILITY_MANIFEST_ID = "aic:campaign5-capability:1.0.0"


class Campaign5Error(ValueError):
    """A Campaign 5 policy object or discovery is outside the frozen profile."""


@dataclass(frozen=True)
class Campaign5Family:
    """One action family lifted onto the official O-RAN path.

    ``scope_fields`` are the identity leaves the caller supplies on the gateway
    command scope; ``value_fields`` are the configuration leaves carried on the
    plan step value.  Their union is exactly the policy ``config`` object, which
    is also the ``observed<...>`` quantity the status schema reports on a
    VERIFIED/MISMATCH readback.
    """

    key: str
    policy_type_id: str
    adapter_name: str
    catalog_action_id: str
    scope_kind: str
    scope_fields: Tuple[str, ...]
    axis: str
    value_fields: Tuple[str, ...]
    observed_key: str
    readback_counter: str
    rc_style: int
    rc_action_id: int
    rc_param_ids: Tuple[int, ...]
    policy_digest: str
    status_digest: str

    @property
    def config_fields(self) -> Tuple[str, ...]:
        return self.scope_fields + self.value_fields


CAMPAIGN5_FAMILIES: Dict[str, Campaign5Family] = {
    "cap": Campaign5Family(
        key="cap",
        policy_type_id="AIC_UeDlPrbCap_1.0.0",
        adapter_name="r1-cap",
        catalog_action_id="ue-dl-prb-cap",
        scope_kind="UE",
        scope_fields=("cellId", "ueId"),
        axis="dlPrbCap",
        value_fields=("maxDlPrbs",),
        observed_key="observedDlPrbCap",
        readback_counter="RAN.UE.DlPrbCap",
        rc_style=2,
        rc_action_id=102,
        rc_param_ids=(211, 212),
        policy_digest="67a8dc22a2ab350723eaa252d134bab52257bb89774c6cbe26f3c6debbe0db29",
        status_digest="69b0cbfec6e38f6de90415dcd8e26acb6e68a6b64218c4e31a37c08e282d55c0",
    ),
    "priority": Campaign5Family(
        key="priority",
        policy_type_id="AIC_SchedulerPriority_1.0.0",
        adapter_name="r1-priority",
        catalog_action_id="scheduler-priority",
        scope_kind="UE",
        scope_fields=("cellId", "ueId"),
        axis="pfWeight",
        value_fields=("pfWeight",),
        observed_key="observedPfWeight",
        readback_counter="RAN.UE.PfWeight",
        rc_style=2,
        rc_action_id=103,
        rc_param_ids=(221, 222),
        policy_digest="e8d4ac066b51439a46c582f8b23bc43ddfd34c38b2424d23a4e6e7de738ae010",
        status_digest="8761d2da0cdecb4a12c2475dafebcece3c91eaa3f7678351399ce6334d9f254d",
    ),
    "mcs": Campaign5Family(
        key="mcs",
        policy_type_id="AIC_DlMcsBounds_1.0.0",
        adapter_name="r1-mcs",
        catalog_action_id="dl-mcs-bounds",
        scope_kind="NRCellDU",
        scope_fields=("cellId",),
        axis="dlMcsBounds",
        value_fields=("minDlMcs", "maxDlMcs"),
        observed_key="observedMcsBounds",
        readback_counter="RAN.Cell.DlMcsBounds",
        rc_style=2,
        rc_action_id=101,
        rc_param_ids=(201, 202, 203),
        policy_digest="31cbe381074e82520ba4564b92214b8485994114a6e6514ed5f50849b1fde6ab",
        status_digest="6b36f46d6147f6c21d0552108bdacdd1825fad5596d80492762d4a7f27549eeb",
    ),
    "power": Campaign5Family(
        key="power",
        policy_type_id="AIC_CellDlTxPower_1.0.0",
        adapter_name="r1-power",
        catalog_action_id="dl-rf-attenuation",
        scope_kind="NRCellDU",
        scope_fields=("cellId", "gnbId"),
        axis="txAttenuationDb",
        value_fields=("txAttenuationDb",),
        observed_key="observedTxAttenuationDb",
        readback_counter="RAN.Cell.TxAttenuationDb",
        rc_style=2,
        rc_action_id=104,
        rc_param_ids=(231, 232, 233),
        policy_digest="ada67b4db5b5943dd722396a5f308c92127db836d1a1fb3c01e3a81f426a961b",
        status_digest="1e8adf35af7d3ee586ed3c265a05899983498f6d375c56e9ad5fb945983cd5b0",
    ),
}

CAMPAIGN5_POLICY_TYPES: Tuple[str, ...] = tuple(
    family.policy_type_id for family in CAMPAIGN5_FAMILIES.values()
)

_FAMILY_BY_TYPE: Dict[str, Campaign5Family] = {
    family.policy_type_id: family for family in CAMPAIGN5_FAMILIES.values()
}


def family_by_policy_type(policy_type_id: str) -> Campaign5Family:
    try:
        return _FAMILY_BY_TYPE[policy_type_id]
    except KeyError as exc:
        raise Campaign5Error(
            f"{policy_type_id} is not a Campaign 5 policy type"
        ) from exc


# -- schema loading (own validator, non-bundle directory) --------------------

_SCHEMA_CACHE: Dict[str, Dict[str, Any]] = {}
_VALIDATOR_CACHE: Dict[str, Draft202012Validator] = {}


def _schema_file(schema_id: str) -> Path:
    # Accept both "AIC_UeDlPrbCap_1.0.0.policy" and the bare "...schema.json" name.
    name = schema_id if schema_id.endswith("schema.json") else f"{schema_id}.schema.json"
    return _SCHEMA_DIR / name


def load_campaign5_schema(schema_id: str) -> Dict[str, Any]:
    """Return the frozen Campaign 5 schema for ``<type>.policy`` / ``.status``."""
    if schema_id not in _SCHEMA_CACHE:
        path = _schema_file(schema_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise Campaign5Error(f"unknown Campaign 5 schema: {schema_id}") from exc
        Draft202012Validator.check_schema(data)
        _SCHEMA_CACHE[schema_id] = data
    return copy.deepcopy(_SCHEMA_CACHE[schema_id])


def _validator(schema_id: str) -> Draft202012Validator:
    if schema_id not in _VALIDATOR_CACHE:
        _VALIDATOR_CACHE[schema_id] = Draft202012Validator(
            load_campaign5_schema(schema_id), format_checker=FormatChecker()
        )
    return _VALIDATOR_CACHE[schema_id]


def validate_campaign5(instance: Any, schema_id: str) -> None:
    """Raise :class:`Campaign5Error` if *instance* violates the named schema."""
    errors = sorted(
        _validator(schema_id).iter_errors(instance),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        path = ".".join(str(item) for item in first.absolute_path) or "$"
        raise Campaign5Error(f"{schema_id}: {path}: {first.message}")


def local_schema_digest(schema_id: str) -> str:
    """The pinned ``jcs_sha256`` of a Campaign 5 schema as it stands on disk."""
    return jcs_sha256(load_campaign5_schema(schema_id))


# -- capability manifest + discovery gate ------------------------------------


def campaign5_capability_manifest() -> Dict[str, Any]:
    """Build the in-repo Campaign 5 capability manifest (Option B).

    Unlike the frozen single-type steering manifest, this one carries a
    per-type ``schemaDigests`` map and a ``ranFunctionDefinitions`` block: the
    capability gate is on the *definition* (the advertised nested RAN-parameter
    tree), not on a bare action number.  Every digest is the tree-pinned
    ``jcs_sha256`` of the on-disk schema, so it agrees by construction with what
    the in-repo producer advertises.
    """
    schema_digests: Dict[str, Dict[str, str]] = {}
    definitions: Dict[str, Dict[str, Any]] = {}
    for family in CAMPAIGN5_FAMILIES.values():
        schema_digests[family.policy_type_id] = {
            "policy": local_schema_digest(f"{family.policy_type_id}.policy"),
            "status": local_schema_digest(f"{family.policy_type_id}.status"),
        }
        definitions[family.policy_type_id] = {
            "ricStyleType": family.rc_style,
            "ricControlActionId": family.rc_action_id,
            "ranParameterIds": list(family.rc_param_ids),
            "scope": family.scope_kind,
        }
    return {
        "manifestId": CAPABILITY_MANIFEST_ID,
        "policyTypes": list(CAMPAIGN5_POLICY_TYPES),
        "schemaDigests": schema_digests,
        "ranFunctionDefinitions": definitions,
    }


def verify_campaign5_discovery(
    discovery: Mapping[str, Any],
    capability_manifest: Mapping[str, Any],
    policy_type_id: str,
) -> None:
    """Refuse unless R1 discovery, the manifest and the local pin all agree.

    Mirrors ``tools/g3ota/composition.py::build_policy_type_discovery``: three
    digests must be byte-equal, and (design section 4.6, BUILDPLAN step 4) the
    advertised RAN-function *definition* must be present -- a style/action number
    without the nested definition is a failed capability gate, not a pass.
    """
    family = family_by_policy_type(policy_type_id)
    schema_digests = (capability_manifest or {}).get("schemaDigests", {})
    advertised = (
        schema_digests.get(policy_type_id)
        if isinstance(schema_digests, Mapping) else None
    )
    if not isinstance(advertised, Mapping):
        raise Campaign5Error(
            f"capability manifest does not advertise {policy_type_id}"
        )
    if policy_type_id not in (capability_manifest or {}).get("policyTypes", []):
        raise Campaign5Error(f"capability manifest lacks policy type {policy_type_id}")

    detail = discovery.get(policy_type_id) if isinstance(discovery, Mapping) else None
    if not isinstance(detail, Mapping):
        raise Campaign5Error(f"R1 discovery lacks policy type {policy_type_id}")

    # The R1 side may advertise inline schemas or a digest; support both.
    if isinstance(detail.get("policySchema"), Mapping):
        r1_policy = jcs_sha256(detail["policySchema"])
        r1_status = jcs_sha256(detail["statusSchema"])
    else:
        r1_policy = detail.get("policySchemaSha256") or detail.get("schemaSha256")
        r1_status = detail.get("statusSchemaSha256")

    local_policy = local_schema_digest(f"{policy_type_id}.policy")
    local_status = local_schema_digest(f"{policy_type_id}.status")
    if (
        r1_policy != local_policy
        or r1_status != local_status
        or advertised.get("policy") != local_policy
        or advertised.get("status") != local_status
    ):
        raise Campaign5Error(
            "R1, capability manifest and pinned Campaign 5 schemas disagree; "
            "refusing rather than translating against one of three"
        )

    # Capability gate on the definition, not the number.
    required = {
        "ricStyleType": family.rc_style,
        "ricControlActionId": family.rc_action_id,
        "ranParameterIds": list(family.rc_param_ids),
    }
    manifest_definitions = (capability_manifest or {}).get(
        "ranFunctionDefinitions", {}
    )
    manifest_def = (
        manifest_definitions.get(policy_type_id)
        if isinstance(manifest_definitions, Mapping) else None
    )
    if not isinstance(manifest_def, Mapping):
        raise Campaign5Error(
            f"{policy_type_id}: capability manifest lacks the RAN-function definition"
        )
    advertised_def = detail.get("ranFunctionDefinition")
    if not isinstance(advertised_def, Mapping):
        raise Campaign5Error(
            f"{policy_type_id}: no advertised RAN-function definition; a "
            "style/action number without the nested definition is a failed "
            "capability gate (BUILDPLAN section 4 step 4)"
        )
    for key, value in required.items():
        if manifest_def.get(key) != value or advertised_def.get(key) != value:
            raise Campaign5Error(
                f"{policy_type_id}: R1/capability RAN-function definition {key} "
                f"does not match the required {value!r}"
            )
