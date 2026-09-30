# 1.0.1 versioning judgement

## Decision

The corrections in `task.md` sections 3 through 6 are contract-package
corrections. They do not change the on-wire policy or status schemas. The
correct release boundary is therefore a package patch release, 1.0.1, while
the wire type remains `AIC_UECellSteering_1.0.0`.

No change to any `AIC_UECellSteering_1.0.0.*` or `aic.*.schema.json` byte is
required:

- materializing already-defined status values, adding the already-required
  `fresh` field, and making the byte-flip boundary unambiguous only correct the
  catalog/runner pairing;
- separating SC-033 history from its current snapshot produces two objects
  accepted by the unchanged frozen status schema;
- O1 rule applicability is runner/catalog metadata, not a wire payload;
- `deliveryBindingId` placement is endpoint-template input in the scenario
  step contract, not a payload or header field.

## SC-033 schema execution

The corrected current-status evidence object
`sc-033.schema-valid-corrected-current-status.json` was loaded from the frozen
1.0.0 failure evidence and checked with
`jsonschema.Draft202012Validator`, including its format checker, against
`AIC_UECellSteering_1.0.0.status.schema.json`.

The executed result was `valid=true`, with zero validation errors. The checked
object has:

- `enforceStatus = NOT_ENFORCED`;
- `enforceReason = OTHER_REASON`;
- `aicStatus.policyState = NOT_ENFORCED`;
- `aicStatus.policyTerminal = false`;
- `aicStatus.error.code = AIC_E2_NOT_READY`;
- a new `aicStatus.statusSeq = 8`; and
- none of `episodeId`, `episodeState`, `episodeTerminal`, `selectedCell`,
  `control`, `readback`, or `rollback`.

This confirms that the corrected current state is representable by the frozen
schema without changing its `$id` or bytes.

## File naming and byte boundary

The corrected package uses patch-version filenames for package-level mutable
authorities:

- `scenario-catalog.1.0.0.json` becomes
  `scenario-catalog.1.0.1.json`;
- `scenario-runner-contract.1.0.0.json` becomes
  `scenario-runner-contract.1.0.1.json`;
- the packaging stage will issue `bundle-manifest.1.0.1.json`; and
- the packaging stage will issue `handoff-manifest.1.0.1.json`.

The C1 bundle deliberately does not create either manifest. Their final file
sets and digests belong to the packaging stage.

The following schema filenames, `$id` values, and bytes remain exactly 1.0.0:

- `AIC_UECellSteering_1.0.0.policy.schema.json`;
- `AIC_UECellSteering_1.0.0.status.schema.json`;
- `aic.policy-evidence-filter.1.0.0.schema.json`;
- `aic.policy-evidence.1.0.0.schema.json`; and
- `aic.ran-capability.1.0.0.schema.json`.

All other copied, unmodified fixtures, XML vectors, and supporting schemas keep
their 1.0.0 filenames and bytes. A copied file is renamed or edited only when a
section 3 through 6 correction requires it. The frozen
`contracts/oran-aic/1.0.0/` tree remains untouched.

## Reproduction

From the repository root, the decisive schema check is:

```sh
python3 - <<'PY'
import json
from pathlib import Path
from jsonschema import Draft202012Validator

schema = json.loads(Path("contracts/oran-aic/1.0.0/shared-contract-bundle/AIC_UECellSteering_1.0.0.status.schema.json").read_text())
instance = json.loads(Path("/home/lics-mini-1/a1p-stage/contract-1.0.0-failure-evidence/conflicts/instances/sc-033.schema-valid-corrected-current-status.json").read_text())
Draft202012Validator.check_schema(schema)
errors = list(Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(instance))
assert not errors, errors
forbidden = {"episodeId", "episodeState", "episodeTerminal", "selectedCell", "control", "readback", "rollback"}
assert forbidden.isdisjoint(instance["aicStatus"])
print("SC-033 corrected current status: VALID")
PY
```
