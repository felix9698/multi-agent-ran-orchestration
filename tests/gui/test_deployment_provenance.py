"""``project_deployment_provenance`` - the pure projection, tested in isolation.

The console must honestly show which composed release a bound deployment
composes against and which objectives it advertises as executable -
``PIN_TO_CELL`` only for a composed-release-bound deployment, with an experimental
extension such as ``E2SM_RC_STYLE2_ACTION6_QOS`` advertised for visibility
but never combined with the frozen A1 UE Cell Steering policy
(``a1_policy_type`` stays ``None`` on that entry).

``LiveIntegration.advertised_objectives()`` (``oran/rapp/gui_entry.py``)
returns ``oran.integration.objectives.AdvertisedComposition`` - a dataclass
with ``.objectives`` (a tuple of ``ObjectiveAdvertisement``: ``kind`` /
``executable`` / ``state`` / ``reason`` / ``provenance``) and ``.extensions``
(a tuple of ``ExtensionAdvertisement``: ``name`` / ``a1_policy_type`` /
``reason``, always non-executable) - not a plain sequence of dicts.  This
module reads that real shape through the real ``oran.integration.objectives``
dataclasses and the real ``advertise()`` function, never by re-describing the
shape as a mock dict: a mock that happens to match today's fields would keep
passing after a real field renamed or restructured, and the whole reason
this module exists is to catch exactly that drift before it reaches the
console.  It also keeps the pre-integration fallback path this projection
was written for: an object with no ``advertised_objectives`` at all still
degrades to ``UNKNOWN`` with a reason, never to an empty, falsely-successful
objective list.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from gui.operator.sources.live import project_deployment_provenance
from oran.integration.objectives import (
    EXECUTABLE_OBJECTIVES,
    AdvertisedComposition,
    ExtensionAdvertisement,
    ObjectiveAdvertisement,
    advertise,
)
from oran.integration.deployment_binding import DeploymentBindingContracts

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = REPO_ROOT / "docs" / "phase-a" / "raw" / "mock-local"
COMPOSED_RELEASE_RECEIPTS = Path.home() / "OranC" / "lower-final-handoff"


def _real_composed_release_root():
    """The extracted composed release root, if a receipt exists on this machine.

    Mirrors ``tests/test_oran_final_composition.py:real_composed_release_root`` -
    not imported from there because that module is a W-COMP-owned test file
    outside this worktree's ownership; this is display-projection test setup,
    not a restatement of its verification logic.
    """
    if not COMPOSED_RELEASE_RECEIPTS.is_dir():
        return None
    for receipt in sorted(COMPOSED_RELEASE_RECEIPTS.iterdir(), reverse=True):
        root = receipt / "oran-aic-lower-integration-1.0.0"
        if (root / "RECEIPT-OK").is_file():
            return root
    return None


def _capability_manifest():
    import json

    return json.loads((DEPLOYMENT / "capability.json").read_text(encoding="utf-8"))


class _NoAdvertising:
    """What a bound ``LiveIntegration`` looks like before the composed-release seam
    lands at all: ``identity()`` only, no ``advertised_objectives``."""

    def identity(self):
        return {"capabilityManifestId": "manifest-1"}


class _WithComposition:
    """Wraps a real ``AdvertisedComposition`` the way ``LiveIntegration``
    does: ``advertised_objectives()`` returns it, and ``identity()`` carries
    its ``.basis`` as ``compositionBasis`` plus an optional binding record."""

    def __init__(self, composition, *, composed_release_binding=None):
        self._composition = composition
        self._composed_release_binding = composed_release_binding

    def identity(self):
        identity = {"capabilityManifestId": "manifest-1",
                    "compositionBasis": self._composition.basis}
        if self._composed_release_binding is not None:
            identity["composedReleaseBinding"] = self._composed_release_binding
        return identity

    def advertised_objectives(self):
        return self._composition


class _Raising:
    def identity(self):
        return {}

    def advertised_objectives(self):
        raise RuntimeError("boom")


class NoDeploymentBound(unittest.TestCase):
    def test_none_integration_is_honestly_unknown(self):
        provenance = project_deployment_provenance(None)
        self.assertIsNone(provenance.composed_release_binding)
        self.assertIsNone(provenance.composition_basis)
        self.assertEqual(provenance.objectives, ())
        self.assertEqual(provenance.objectives_status, "UNKNOWN")
        self.assertIn("no deployment is bound", provenance.objectives_reason)


class TheSeamIsAbsent(unittest.TestCase):
    """A bound integration whose build predates ``advertised_objectives``."""

    def test_a_bound_integration_without_the_method_is_unknown_not_empty(self):
        provenance = project_deployment_provenance(_NoAdvertising())
        self.assertEqual(provenance.objectives, ())
        self.assertEqual(provenance.objectives_status, "UNKNOWN")
        self.assertIn("not available", provenance.objectives_reason)
        self.assertIsNone(provenance.composed_release_binding)
        self.assertIsNone(provenance.composition_basis)

    def test_a_raising_advertiser_is_unknown_with_the_exception_named(self):
        provenance = project_deployment_provenance(_Raising())
        self.assertEqual(provenance.objectives_status, "UNKNOWN")
        self.assertIn("RuntimeError", provenance.objectives_reason)


class ARealUnboundComposition(unittest.TestCase):
    """``advertise()`` itself, with no composed release - the development path
    every profile in this repo's fixtures actually exercises."""

    def test_a_development_deployment_advertises_by_its_own_manifest_alone(self):
        composition = advertise(capability_manifest={"objectives": ["PIN_TO_CELL"]},
                                lower=None)
        self.assertIsInstance(composition, AdvertisedComposition)
        self.assertEqual(composition.basis,
                         "DEVELOPMENT_DEPLOYMENT_NOT_COMPOSED_RELEASE_BOUND")
        self.assertFalse(composition.composed_release_bound)

        provenance = project_deployment_provenance(_WithComposition(composition))
        self.assertEqual(provenance.objectives_status, "OK")
        self.assertIsNone(provenance.composed_release_binding)
        self.assertEqual(provenance.composition_basis,
                         "DEVELOPMENT_DEPLOYMENT_NOT_COMPOSED_RELEASE_BOUND")
        by_name = {item.objective: item for item in provenance.objectives}
        self.assertTrue(by_name["PIN_TO_CELL"].executable)

    def test_a_declared_but_unadvertised_objective_carries_its_own_reason(self):
        composition = advertise(
            capability_manifest={"objectives": ["BALANCE_PRB_LOAD"]}, lower=None)
        provenance = project_deployment_provenance(_WithComposition(composition))
        by_name = {item.objective: item for item in provenance.objectives}
        # PIN_TO_CELL is schema-representable but this manifest never
        # declared it - the real advertise() reports that distinctly from
        # "not executable at all".
        self.assertFalse(by_name["PIN_TO_CELL"].executable)
        self.assertIn("does not advertise", by_name["PIN_TO_CELL"].reason)

    def test_manually_built_dataclasses_carry_the_extension_shape_too(self):
        """Not a mock dict: the real frozen dataclasses, built directly, to
        pin the extension mapping (a1_policy_type stays None) independently
        of whatever a live ``advertise()`` call happens to return today."""
        composition = AdvertisedComposition(
            objectives=(ObjectiveAdvertisement(
                kind="PIN_TO_CELL", executable=True, state="EXECUTABLE"),),
            extensions=(ExtensionAdvertisement(
                name="E2SM_RC_STYLE2_ACTION6_QOS", state="EXPERIMENTAL",
                service_model="E2SM-RC", control_axis="qos",
                a1_policy_type=None,
                reason="capability and provenance only"),),
            composed_release_bound=True, basis="COMPOSED_RELEASE_BOUND:oran-aic-lower-integration/1.0.0@abc")
        provenance = project_deployment_provenance(_WithComposition(
            composition, composed_release_binding={
                "release": "oran-aic-lower-integration/1.0.0",
                "tag": "oran-aic-lower-integration-1.0.0", "commit": "9f607c336a6f"}))
        self.assertEqual(provenance.objectives_status, "OK")
        self.assertEqual(len(provenance.objectives), 2)
        by_name = {item.objective: item for item in provenance.objectives}
        self.assertTrue(by_name["PIN_TO_CELL"].executable)
        extension = by_name["E2SM_RC_STYLE2_ACTION6_QOS"]
        self.assertFalse(extension.executable)
        self.assertIsNone(extension.a1_policy_type)
        self.assertEqual(extension.reason, "capability and provenance only")
        self.assertIn("oran-aic-lower-integration/1.0.0",
                      provenance.composed_release_binding)
        self.assertIn("9f607c336a6f", provenance.composed_release_binding)


class ARealComposedReleaseBoundComposition(unittest.TestCase):
    """The genuine article: the real extracted composed release on this
    machine, the real ``DeploymentBindingContracts.load``, and the real
    ``advertise()`` - not a synthetic or hand-built stand-in.  Skips, rather
    than fabricates a release, on a machine with no fresh-recipient
    extraction present.
    """

    def test_a_real_bound_deployment_advertises_pin_to_cell_alone(self):
        root = _real_composed_release_root()
        if root is None:
            self.skipTest("no fresh-recipient extraction of the composed "
                          "release is present on this machine")
        lower = DeploymentBindingContracts.load(root)
        composition = advertise(capability_manifest=_capability_manifest(),
                                lower=lower)
        self.assertTrue(composition.composed_release_bound)
        self.assertEqual(composition.executable, EXECUTABLE_OBJECTIVES)

        provenance = project_deployment_provenance(_WithComposition(
            composition, composed_release_binding=lower.binding()))

        self.assertEqual(provenance.objectives_status, "OK")
        self.assertTrue(provenance.composition_basis.startswith("COMPOSED_RELEASE_BOUND:"))
        self.assertIsNotNone(provenance.composed_release_binding)
        self.assertIn(lower.identity.release, provenance.composed_release_binding)
        by_name = {item.objective: item for item in provenance.objectives}
        self.assertTrue(by_name["PIN_TO_CELL"].executable)
        self.assertFalse(by_name["BALANCE_PRB_LOAD"].executable)
        # The experimental extension the real release declares comes back as
        # an extension, never selectable, never carrying an A1 policy type.
        extension_names = {item.objective for item in provenance.objectives
                           if item.a1_policy_type is None
                           and item.objective not in composition.executable}
        self.assertIn("E2SM_RC_STYLE2_ACTION6_QOS", extension_names)


class ANonSequenceReturn(unittest.TestCase):
    def test_a_non_sequence_non_composition_return_is_unknown_not_a_crash(self):
        class _Bare:
            def identity(self):
                return {}

            def advertised_objectives(self):
                return object()

        provenance = project_deployment_provenance(_Bare())
        self.assertEqual(provenance.objectives_status, "UNKNOWN")
        self.assertTrue(provenance.objectives_reason)


if __name__ == "__main__":
    unittest.main()
