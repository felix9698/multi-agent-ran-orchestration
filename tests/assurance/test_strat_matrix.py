"""The six strategies, behind one interface, over one set of views.

Authority: ``task_0820_unified_ota_assurance_system.md`` section 11 ("동일
Kernel, contracts, candidate catalog, harm limits, evidence rules와 hardware
조건 아래 다음 Evidence Coordinator strategy를 선택할 수 있게 한다"), design
section 12.

This file is the matrix itself: every claim below is made about *all six*
kinds, driven off :data:`~assurance.advisors.strategies.registry.STRATEGY_CLASSES`
rather than a list written out here, so a seventh strategy added without
satisfying them fails immediately rather than quietly opting out.
"""

from __future__ import annotations

import inspect
import json
import unittest

from assurance.advisors.messages import AdvisoryKind, AdvisoryMessage
from assurance.advisors.roles import EvidenceCoordinator
from assurance.advisors.strategies import (
    LLM_STRATEGY_KINDS,
    STRATEGY_CLASSES,
    STRATEGY_DESCRIPTION_KEYS,
    AdversarialAdvisorStrategy,
    DeterministicStrategy,
    MonolithicLLMStrategy,
    OptimizationStrategy,
    RandomStrategy,
    RoleSeparatedLLMCoordinator,
    StrategyConfigurationError,
    build_strategy,
)
from assurance.advisors.strategies.description import is_credential_key
from assurance.advisors.strategy import AdvisoryStrategy, StrategyKind
from assurance.core.components import ComponentId

from tests.assurance.strategy_support import (
    AVAILABLE_IDS,
    CORRELATION,
    EPOCH,
    EXHAUSTED_BUDGET,
    role_separated_script,
    scripted,
    views,
)


def _build(kind: StrategyKind):
    """One instance of *kind*, configured the way a batch plan would."""
    if kind is StrategyKind.RANDOM:
        return build_strategy(kind, seed=7)
    if kind is StrategyKind.LLM_EVIDENCE_COORDINATOR:
        return build_strategy(kind, transport=scripted(role_separated_script()))
    if kind is StrategyKind.MONOLITHIC_LLM:
        return build_strategy(kind, transport=scripted([role_separated_script()[-1]]))
    return build_strategy(kind)


class TheFamilyIsComplete(unittest.TestCase):
    """Six kinds, six implementations, nothing missing and nothing extra."""

    def test_every_design_section_12_strategy_has_an_implementation(self) -> None:
        self.assertEqual(set(STRATEGY_CLASSES), set(StrategyKind))
        self.assertEqual(len(STRATEGY_CLASSES), 6)

    def test_each_class_reports_the_kind_it_is_registered_under(self) -> None:
        for kind in StrategyKind:
            with self.subTest(kind=kind.value):
                self.assertIs(_build(kind).strategy_kind, kind)

    def test_the_old_coordinator_is_not_among_them(self) -> None:
        # Design section 2.2 and task section 11: "기존 Coordinator는 비교
        # 전략으로 포함하지 않는다."  Named here so the exclusion is a test
        # rather than an omission nobody would notice being reversed.
        names = {cls.__name__ for cls in STRATEGY_CLASSES.values()}
        self.assertNotIn("IntentCoordinator", names)
        for name in names:
            self.assertFalse(name.startswith("Legacy"), name)

    def test_only_the_two_llm_kinds_take_a_transport(self) -> None:
        self.assertEqual(
            set(LLM_STRATEGY_KINDS),
            {StrategyKind.LLM_EVIDENCE_COORDINATOR, StrategyKind.MONOLITHIC_LLM},
        )


class EveryStrategySatisfiesTheOneInterface(unittest.TestCase):
    """Design section 12: "replaceable through one typed interface"."""

    def test_each_is_a_runtime_checkable_advisory_strategy(self) -> None:
        for kind in StrategyKind:
            with self.subTest(kind=kind.value):
                self.assertIsInstance(_build(kind), AdvisoryStrategy)

    def test_each_propose_takes_exactly_the_frozen_parameters(self) -> None:
        expected = {
            "catalog_view",
            "evidence_view",
            "budget_view",
            "correlation_id",
            "epoch_hash",
            "now",
        }
        for kind in StrategyKind:
            with self.subTest(kind=kind.value):
                signature = inspect.signature(_build(kind).propose)
                self.assertEqual(set(signature.parameters), expected)
                for parameter in signature.parameters.values():
                    self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_each_matches_the_evidence_coordinator_role_signature(self) -> None:
        # The proposed method is one strategy among six, not a privileged case
        # with extra inputs: ``propose`` and ``propose_next`` take the same
        # parameters, so any strategy can back the role.
        role = set(inspect.signature(EvidenceCoordinator.propose_next).parameters) - {"self"}
        for kind in StrategyKind:
            with self.subTest(kind=kind.value):
                self.assertEqual(
                    set(inspect.signature(_build(kind).propose).parameters), role
                )

    def test_each_describe_returns_the_same_key_set(self) -> None:
        # Task section 11's "controlled equivalent budget" is only checkable
        # if every strategy states its budget in the same fields.
        for kind in StrategyKind:
            with self.subTest(kind=kind.value):
                described = _build(kind).describe()
                self.assertTrue(STRATEGY_DESCRIPTION_KEYS.issubset(set(described)))

    def test_no_description_carries_a_credential(self) -> None:
        # A description is written into the run record, and the run record is
        # an export: the constraint list forbids an actual credential landing
        # in one.  ``tokenBudget`` is a count and is one of the nine frozen
        # fields, which is why the check is name-aware rather than a bare
        # substring scan for "token".
        for kind in StrategyKind:
            described = _build(kind).describe()
            for name in described:
                with self.subTest(kind=kind.value, field=name):
                    self.assertFalse(
                        is_credential_key(name), f"{kind.value} describes {name!r}"
                    )
            rendered = json.dumps(described, default=str)
            for leak in ("sk-", "Bearer ", "-----BEGIN"):
                with self.subTest(kind=kind.value, leak=leak):
                    self.assertNotIn(leak, rendered)


class EveryStrategyProducesTheSameKernelInput(unittest.TestCase):
    """The Kernel-facing contract does not vary with the strategy.

    Design section 12: "All strategies use the same Kernel, contracts,
    catalog, harm limits, hardware conditions, and evidence rules."  The
    adversarial baseline is excluded here on purpose -- breaking this contract
    is what it is for, and ``test_strat_adversarial.py`` asserts the system
    refuses it.
    """

    HONEST_KINDS = tuple(
        kind for kind in StrategyKind if kind is not StrategyKind.ADVERSARIAL_ADVISOR
    )

    def test_each_proposes_a_well_formed_next_candidate_proposal(self) -> None:
        for kind in self.HONEST_KINDS:
            with self.subTest(kind=kind.value):
                message = _build(kind).propose(**views())
                self.assertIsInstance(message, AdvisoryMessage)
                self.assertIs(message.kind, AdvisoryKind.NEXT_CANDIDATE_PROPOSAL)
                self.assertIs(message.issued_by, ComponentId.EVIDENCE_COORDINATOR)
                self.assertEqual(message.correlation_id, CORRELATION)
                self.assertEqual(message.epoch_hash, EPOCH)

    def test_each_names_only_a_candidate_the_catalog_makes_available(self) -> None:
        for kind in self.HONEST_KINDS:
            with self.subTest(kind=kind.value):
                message = _build(kind).propose(**views())
                self.assertIn(message.body.candidate_id, AVAILABLE_IDS)

    def test_each_returns_none_once_the_kernel_budget_is_exhausted(self) -> None:
        # Design section 8: "No agent can keep a case alive past" the caps.
        for kind in self.HONEST_KINDS:
            with self.subTest(kind=kind.value):
                self.assertIsNone(
                    _build(kind).propose(**views(budget_view=EXHAUSTED_BUDGET))
                )

    def test_each_returns_none_when_nothing_is_available(self) -> None:
        for kind in self.HONEST_KINDS:
            with self.subTest(kind=kind.value):
                self.assertIsNone(_build(kind).propose(**views(catalog_view=())))

    def test_no_proposal_carries_an_admissible_quantity(self) -> None:
        # Every TypedQuantity reachable from an advisory must be DRAFT; the
        # message type enforces it, and a proposal built by any strategy has
        # to still satisfy it.
        for kind in self.HONEST_KINDS:
            with self.subTest(kind=kind.value):
                body = _build(kind).propose(**views()).body
                self.assertIsNone(body.expected_information_gain)


class TheRegistryFailsClosed(unittest.TestCase):
    """A misconfigured plan is refused, never defaulted."""

    def test_random_without_a_seed_is_refused(self) -> None:
        with self.assertRaises(StrategyConfigurationError):
            build_strategy(StrategyKind.RANDOM)

    def test_an_llm_strategy_without_a_transport_is_refused(self) -> None:
        for kind in LLM_STRATEGY_KINDS:
            with self.subTest(kind=kind.value):
                with self.assertRaises(StrategyConfigurationError):
                    build_strategy(kind)

    def test_a_non_llm_strategy_given_a_transport_is_refused(self) -> None:
        # The one that protects the comparison: a plan must not believe it
        # configured a model arm while running an LLM-free one.
        with self.assertRaises(StrategyConfigurationError):
            build_strategy(StrategyKind.DETERMINISTIC, transport=scripted([]))

    def test_a_seed_on_a_strategy_that_draws_none_is_refused(self) -> None:
        with self.assertRaises(StrategyConfigurationError):
            build_strategy(StrategyKind.OPTIMIZATION, seed=1)

    def test_a_token_budget_on_a_model_free_strategy_is_refused(self) -> None:
        from assurance.advisors.strategies import DEFAULT_COMPARABLE_BUDGET

        with self.assertRaises(StrategyConfigurationError):
            build_strategy(StrategyKind.DETERMINISTIC, budget=DEFAULT_COMPARABLE_BUDGET)

    def test_a_non_strategy_kind_is_refused(self) -> None:
        with self.assertRaises(StrategyConfigurationError):
            build_strategy("DETERMINISTIC")  # type: ignore[arg-type]

    def test_the_registry_builds_the_class_it_advertises(self) -> None:
        expected = {
            StrategyKind.LLM_EVIDENCE_COORDINATOR: RoleSeparatedLLMCoordinator,
            StrategyKind.DETERMINISTIC: DeterministicStrategy,
            StrategyKind.RANDOM: RandomStrategy,
            StrategyKind.OPTIMIZATION: OptimizationStrategy,
            StrategyKind.MONOLITHIC_LLM: MonolithicLLMStrategy,
            StrategyKind.ADVERSARIAL_ADVISOR: AdversarialAdvisorStrategy,
        }
        for kind, cls in expected.items():
            with self.subTest(kind=kind.value):
                self.assertIsInstance(_build(kind), cls)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
