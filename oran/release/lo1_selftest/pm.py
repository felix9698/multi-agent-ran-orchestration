"""PM file generation for the contract-faithful Provider emulator.

``emulator-boundary.1.0.0.json#/liveValueDivergence`` is the whole point of this
module.  ``RULE-O1-LIVE-VALUE-INVARIANTS`` forbids a live measurement value
from being equal to any golden sample value, so an emulator that replays
``golden/o1/valid-prb.xml`` verbatim would emit a file the live rule must
reject.  The generator therefore:

* reuses the golden document's **structure** -- element names, nesting,
  attribute set and order are taken from the golden bytes, so a golden change
  changes the emitted document;
* re-tags that structure under the namespace and stylesheet the **PM file
  profile** declares, so a profile change also changes the emitted document
  (``LO1-ST-E06``);
* generates values inside the ranges the profile declares, from a seeded
  generator, that are provably different from every golden sample value;
* derives the measurement window from the run's observed clock, never from a
  golden timestamp.

Two consecutive runs therefore produce different PM digests, which is exactly
what ``rawDigestComparedOnlyToCapturedBytes: true`` expects.
"""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree

from .frozen import FrozenBundle

#: Fault names the emulator may inject into a generated PM document.  Each one
#: exists to drive a named falsifier and none of them is reachable on a clean
#: self-test run.
FAULT_VALUE_OUT_OF_RANGE = "PM_VALUE_OUT_OF_RANGE"
FAULT_VALUE_EQUALS_GOLDEN = "PM_VALUE_EQUALS_GOLDEN"
FAULT_DN_COLLISION = "PM_DN_COLLISION"
FAULT_SUSPECT_SAMPLE = "PM_SUSPECT_SAMPLE"
FAULT_NULL_VALUE = "PM_NULL_VALUE"

PM_FAULTS = frozenset({
    FAULT_VALUE_OUT_OF_RANGE,
    FAULT_VALUE_EQUALS_GOLDEN,
    FAULT_DN_COLLISION,
    FAULT_SUSPECT_SAMPLE,
    FAULT_NULL_VALUE,
})


class PmGenerationError(RuntimeError):
    """The generator could not honour the frozen profile; it never guesses."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _retag(element: ElementTree.Element, namespace: str) -> ElementTree.Element:
    clone = ElementTree.Element(f"{{{namespace}}}{_local(element.tag)}", dict(element.attrib))
    clone.text = element.text
    clone.tail = element.tail
    for child in element:
        clone.append(_retag(child, namespace))
    return clone


def _find(element: ElementTree.Element, local: str) -> ElementTree.Element:
    for child in element.iter():
        if _local(child.tag) == local:
            return child
    raise PmGenerationError(f"the golden PM structure carries no <{local}>")


def _find_all(element: ElementTree.Element, local: str) -> list[ElementTree.Element]:
    return [child for child in element.iter() if _local(child.tag) == local]


@dataclass(frozen=True)
class GeneratedSample:
    """One generated measurement, as the emulator knows it."""

    measured_object_dn: str
    measurement_name: str
    position: int
    text: str
    suspect: bool

    def numeric(self) -> float | None:
        try:
            return float(self.text)
        except ValueError:
            return None


@dataclass
class GeneratedPmFile:
    raw: bytes
    file_name: str
    window_start: str
    window_end: str
    samples: list[GeneratedSample] = field(default_factory=list)
    golden_value_collisions: tuple[str, ...] = ()


class PmGenerator:
    """Emits TS 32.435 documents from frozen bytes, never from a literal."""

    def __init__(self, bundle: FrozenBundle, *, seed: int) -> None:
        self.bundle = bundle
        self._random = random.Random(seed)
        self._golden_values = bundle.golden_sample_values()
        self._golden_instants = bundle.golden_window_instants()
        self._emitted = 0

    # ------------------------------------------------------------ value picks

    def _pick_integer(self, profile: Mapping[str, Any]) -> str:
        low = int(profile.get("minimum", 0))
        high = int(profile.get("maximum", low + 100))
        candidates = [value for value in range(low, high + 1)
                      if str(value) not in self._golden_values]
        if not candidates:
            raise PmGenerationError(
                f"no value in [{low}, {high}] differs from every golden sample")
        return str(self._random.choice(candidates))

    def _pick_real(self, profile: Mapping[str, Any]) -> str:
        low = float(profile.get("minimum", 0.0))
        high = float(profile.get("maximum", low + 20000.0))
        for _ in range(512):
            value = round(self._random.uniform(low, high), 1)
            text = f"{value:.1f}"
            if text not in self._golden_values:
                return text
        raise PmGenerationError("could not pick a real that differs from every golden sample")

    def _pick(self, name: str) -> str:
        profile = self.bundle.measurement_profile(name)
        kind = str(profile.get("valueKind", "INTEGER"))
        if kind == "INTEGER":
            return self._pick_integer(profile)
        if kind == "REAL":
            return self._pick_real(profile)
        raise PmGenerationError(f"unsupported valueKind {kind!r} for {name!r}")

    # -------------------------------------------------------------- generation

    def generate(
        self,
        *,
        window_start: str,
        window_end: str,
        cell_dns: Sequence[str],
        job_id: str,
        faults: frozenset[str] = frozenset(),
    ) -> GeneratedPmFile:
        if not cell_dns:
            raise PmGenerationError("at least one measured-object DN is required")
        unknown = faults - PM_FAULTS
        if unknown:
            raise PmGenerationError(f"unknown PM fault(s): {sorted(unknown)}")

        namespace = self.bundle.pm_namespace()
        golden = ElementTree.fromstring(
            self.bundle.raw("golden/o1/valid-prb.xml").decode("utf-8"))
        root = _retag(golden, namespace)

        # ---- header / footer: window derived from the run's observed clock.
        _find(root, "measCollec").set("beginTime", window_start)
        footer = _find(_find_all(root, "fileFooter")[0], "measCollec")
        footer.set("endTime", window_end)
        _find(root, "granPeriod").set("endTime", window_end)
        _find(root, "job").set("jobId", job_id)

        for instant in (window_start, window_end):
            if instant in self._golden_instants:
                raise PmGenerationError(
                    "the generated window collides with a golden timestamp; the clock "
                    "must be the run's observed clock, not a golden value")

        # ---- measType vector: positions read from the profile, order from golden.
        meas_info = _find(root, "measInfo")
        names = self.bundle.measurement_names()
        for element in _find_all(meas_info, "measType"):
            meas_info.remove(element)
        template_type = _find(golden, "measType")
        for position, name in enumerate(names, start=1):
            element = ElementTree.SubElement(
                meas_info, f"{{{namespace}}}measType", {"p": str(position)})
            element.text = name
            element.tail = template_type.tail
        # keep the golden ordering: measType elements precede measValue elements
        children = list(meas_info)
        ordered = [child for child in children if _local(child.tag) != "measType"]
        types = [child for child in children if _local(child.tag) == "measType"]
        insert_at = 0
        for index, child in enumerate(ordered):
            if _local(child.tag) == "measValue":
                insert_at = index
                break
            insert_at = index + 1
        for element in list(meas_info):
            meas_info.remove(element)
        for element in ordered[:insert_at] + types + ordered[insert_at:]:
            meas_info.append(element)

        # ---- measValue blocks: one per policy cell.
        template_value = copy.deepcopy(_find(golden, "measValue"))
        template_value = _retag(template_value, namespace)
        for element in _find_all(meas_info, "measValue"):
            meas_info.remove(element)

        samples: list[GeneratedSample] = []
        collisions: list[str] = []
        for index, dn in enumerate(cell_dns):
            block = copy.deepcopy(template_value)
            effective_dn = cell_dns[0] if FAULT_DN_COLLISION in faults else dn
            block.set("measObjLdn", effective_dn)
            results = [child for child in block if _local(child.tag) == "r"]
            for element in results:
                block.remove(element)
            suspect_element = None
            for child in list(block):
                if _local(child.tag) == "suspect":
                    suspect_element = child
                    block.remove(child)
            suspect = FAULT_SUSPECT_SAMPLE in faults and index == 0
            for position, name in enumerate(names, start=1):
                text = self._pick(name)
                if position == 1:
                    if FAULT_VALUE_OUT_OF_RANGE in faults and index == 0:
                        profile = self.bundle.measurement_profile(name)
                        text = str(int(profile.get("maximum", 100)) + 7)
                    elif FAULT_VALUE_EQUALS_GOLDEN in faults and index == 0:
                        text = sorted(
                            value for value in self._golden_values
                            if value.isdigit())[0]
                    elif FAULT_NULL_VALUE in faults and index == 0:
                        text = "NIL"
                element = ElementTree.SubElement(
                    block, f"{{{namespace}}}r", {"p": str(position)})
                element.text = text
                element.tail = results[0].tail if results else None
                samples.append(GeneratedSample(
                    measured_object_dn=effective_dn,
                    measurement_name=name,
                    position=position,
                    text=text,
                    suspect=suspect,
                ))
                if text in self._golden_values:
                    collisions.append(f"{name}@{effective_dn}={text}")
            if suspect_element is not None:
                marker = ElementTree.SubElement(
                    block, f"{{{namespace}}}suspect", {})
                marker.text = "true" if suspect else "false"
                marker.tail = suspect_element.tail
            meas_info.append(block)

        ElementTree.indent(root, space="  ")
        # ``default_namespace=`` rejects unqualified attribute names, and the
        # golden structure is full of them, so the default prefix is registered
        # instead.  The emitted root therefore carries xmlns="<profile value>".
        ElementTree.register_namespace("", namespace)
        body = ElementTree.tostring(root, encoding="unicode")
        document = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + self.bundle.pm_stylesheet_pi()
            + "\n"
            + body.rstrip()
            + "\n"
        )
        self._emitted += 1
        file_name = (
            f"A{window_start.replace(':', '').replace('-', '')}"
            f"-{window_end.replace(':', '').replace('-', '')}"
            f"_{job_id}_{self._emitted:04d}.xml"
        )
        return GeneratedPmFile(
            raw=document.encode("utf-8"),
            file_name=file_name,
            window_start=window_start,
            window_end=window_end,
            samples=samples,
            golden_value_collisions=tuple(collisions),
        )
