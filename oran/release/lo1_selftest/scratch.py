"""Scratch copies of the frozen bundle, and the drift mutations applied to them.

``emulator-boundary.1.0.0.json#/driftDetection/scratchCopyRule`` is absolute:
every drift check operates on a COPY of the bundle in a temporary directory and
the repository bundle is never written.  ``G-CONTRACT-1`` proves a zero-file
diff after the suite, so this module also carries the digest snapshot the
proof is taken over.

Nothing here mutates the oracle in place either: ``LO1-ST-O01`` mutates
``/scenarios/83/expected`` in a scratch copy precisely so that the harness's
*reading* of the oracle can be falsified without touching the authority bytes.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .frozen import (
    ASSIGNMENT,
    CATALOG,
    FrozenBundle,
    NETCONF_PROFILE,
    PA_FILE_PROFILE,
    json_pointer,
    sha256_file,
)


class ScratchError(RuntimeError):
    """A scratch mutation could not be applied, so the check cannot be trusted."""


def snapshot_digests(root: Path) -> dict[str, str]:
    """Relative path -> SHA-256 for every file below ``root``."""
    root = Path(root)
    return {
        str(path.relative_to(root)): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def diff_digests(before: Mapping[str, str], after: Mapping[str, str]) -> tuple[str, ...]:
    changed = [name for name in sorted(set(before) | set(after))
               if before.get(name) != after.get(name)]
    return tuple(changed)


def copy_bundle(source: Path, destination: Path) -> Path:
    """Copy the frozen bundle into ``destination``; the source is never touched."""
    source = Path(source)
    destination = Path(destination)
    if destination.exists():
        raise ScratchError(f"scratch destination already exists: {destination}")
    shutil.copytree(source, destination)
    return destination


def _write_json(path: Path, document: Any) -> None:
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _mutate_json(root: Path, name: str, mutate: Callable[[Any], None]) -> None:
    path = Path(root) / name
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    _write_json(path, document)


# --------------------------------------------------------------------- oracle


def mutate_expected_status(root: Path, *, scenario_pointer: str) -> str:
    """Change one status in ``expected.httpSequence``.  ``LO1-ST-O01``."""
    detail = {}

    def apply(document: Any) -> None:
        expected = json_pointer(document, f"{scenario_pointer}/expected")
        sequence = expected["httpSequence"]
        detail["before"] = list(sequence)
        sequence[0] = 299
        detail["after"] = list(sequence)

    _mutate_json(root, CATALOG, apply)
    return f"httpSequence[0] {detail['before'][0]} -> {detail['after'][0]}"


def mutate_expected_counter(root: Path, *, scenario_pointer: str, member: str) -> str:
    """Change one counter in ``expected``.  ``LO1-ST-O01``."""
    detail = {}

    def apply(document: Any) -> None:
        expected = json_pointer(document, f"{scenario_pointer}/expected")
        if member not in expected:
            raise ScratchError(f"expected has no member {member!r}")
        detail["before"] = expected[member]
        expected[member] = int(expected[member]) + 1
        detail["after"] = expected[member]

    _mutate_json(root, CATALOG, apply)
    return f"{member} {detail['before']} -> {detail['after']}"


def mutate_step_id(root: Path, *, scenario_pointer: str, index: int) -> str:
    """Rename one materialization step.  ``LO1-ST-O01``."""
    detail = {}

    def apply(document: Any) -> None:
        steps = json_pointer(document, f"{scenario_pointer}/materialization/steps")
        detail["before"] = steps[index]["id"]
        steps[index]["id"] = steps[index]["id"] + "-mutated"
        detail["after"] = steps[index]["id"]

    _mutate_json(root, CATALOG, apply)
    return f"steps[{index}].id {detail['before']} -> {detail['after']}"


def mutate_step_expected_status(root: Path, *, scenario_pointer: str, index: int) -> str:
    detail = {}

    def apply(document: Any) -> None:
        steps = json_pointer(document, f"{scenario_pointer}/materialization/steps")
        detail["before"] = steps[index].get("expectedHttpStatus")
        steps[index]["expectedHttpStatus"] = 299
        detail["after"] = 299

    _mutate_json(root, CATALOG, apply)
    return f"steps[{index}].expectedHttpStatus {detail['before']} -> 299"


def mutate_catalog_pointer(root: Path, *, assignment_index: int) -> str:
    """Point the SC-084 assignment at a different catalog entry."""
    detail = {}

    def apply(document: Any) -> None:
        entry = document["assignments"][assignment_index]
        detail["before"] = entry["catalogPointer"]
        entry["catalogPointer"] = "/scenarios/0"
        detail["after"] = entry["catalogPointer"]

    _mutate_json(root, ASSIGNMENT, apply)
    return f"catalogPointer {detail['before']} -> {detail['after']}"


# ---------------------------------------------------------------------- drift


def mutate_golden_netconf_fixture(root: Path, *, name: str) -> str:
    """Flip exactly one byte of a registered golden RPC fixture.  ``LO1-ST-E04``."""
    path = Path(root) / "golden" / "o1" / "netconf" / name
    if not path.is_file():
        raise ScratchError(f"no golden fixture named {name!r}")
    raw = bytearray(path.read_bytes())
    # The message-id digit: a one-byte change that keeps the file well-formed
    # XML, so a byte-driven emulator refuses it while a shape-driven one would
    # happily answer.
    marker = raw.find(b'message-id="')
    if marker < 0:
        raise ScratchError(f"{name} carries no message-id to perturb")
    offset = marker + len(b'message-id="')
    before = chr(raw[offset])
    raw[offset] = ord("9") if before != "9" else ord("8")
    path.write_bytes(bytes(raw))
    return f"{name} message-id first digit {before} -> {chr(raw[offset])} (1 byte)"


def mutate_netconf_profile_capability(root: Path) -> str:
    """Drop one required NETCONF capability.  ``LO1-ST-E05``."""
    detail = {}

    def apply(document: Any) -> None:
        capabilities = document["transport"]["requiredCapabilities"]
        detail["dropped"] = capabilities[-1]
        del capabilities[-1]

    _mutate_json(root, NETCONF_PROFILE, apply)
    return f"requiredCapabilities dropped {detail['dropped']}"


def mutate_pa_file_namespace(root: Path) -> str:
    """Change the PM document namespace.  ``LO1-ST-E06``."""
    detail = {}

    def apply(document: Any) -> None:
        delivery = document["delivery"]
        detail["before"] = delivery["xmlNamespace"]
        delivery["xmlNamespace"] = delivery["xmlNamespace"] + "#drifted"
        detail["after"] = delivery["xmlNamespace"]

    _mutate_json(root, PA_FILE_PROFILE, apply)
    return f"delivery.xmlNamespace {detail['before']} -> {detail['after']}"


def mutate_pa_file_measurement_range(root: Path) -> str:
    """Widen the RRU.PrbDl range so an out-of-contract value can be emitted."""
    detail = {}

    def apply(document: Any) -> None:
        for measurement in document["measurements"]:
            if measurement["name"] == "RRU.PrbDl":
                detail["before"] = measurement["maximum"]
                measurement["maximum"] = 100000
                detail["after"] = measurement["maximum"]
                return
        raise ScratchError("the PM file profile declares no RRU.PrbDl")

    _mutate_json(root, PA_FILE_PROFILE, apply)
    return f"RRU.PrbDl maximum {detail['before']} -> {detail['after']}"


@dataclass(frozen=True)
class ScratchBundle:
    """A mutated copy of the bundle plus a human-readable note of the mutation."""

    root: Path
    mutation: str

    def open(self) -> FrozenBundle:
        return FrozenBundle(self.root)
