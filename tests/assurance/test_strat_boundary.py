"""The advisory boundary around the LLM adapter, and the one conditional smoke.

Authority: ``task_0820_unified_ota_assurance_system.md`` section 11 item 5
("decision/llm_backend를 advisory 전용으로 감싸는 어댑터 -- actuator·ledger
접근 구조적 불가 ... 테스트는 mock LLM으로 hermetic -- 실 API 호출은 별도
조건부 스모크로만"), ``docs/architecture/GATE1-MAP.md`` (``decision/llm_backend.py``
is ``REUSE`` on condition that "backend selection and transport abstraction
are not an actuator; all outputs must be constrained to the Kernel mailbox").

"Structurally impossible" is the claim, so the tests are structural: what the
transport's public surface contains, what a strategy's constructor accepts,
and what a strategy's attributes reference.  A test that merely called the
adapter and observed that nothing bad happened would prove nothing about the
paths nobody exercised.

The last class is the exception that proves the rule.  Exactly one test here
can touch a real provider, and it is skipped unless a run opts in explicitly
-- an API key in the environment is normal on a developer machine, so keying
the smoke on the key alone would silently make the suite non-hermetic.
"""

from __future__ import annotations

import os
import unittest

from assurance.advisors.strategies import (
    ADVISORY_TRANSPORT_SURFACE,
    AdvisoryCompletion,
    AdvisoryTransport,
    LLMBackendTransport,
    MonolithicLLMStrategy,
    RoleSeparatedLLMCoordinator,
    ScriptedTransport,
    TransportError,
    available_transport,
    prompt_digest,
)
from assurance.advisors.strategies.description import is_credential_key
from assurance.core.addressing import is_content_hash

from tests.assurance.strategy_support import choice_reply, role_separated_script, scripted, views

#: The opt-in for the one test that may reach a provider.  Two conditions, not
#: one: the environment variable *and* a usable backend.
SMOKE_ENV = "AIC_ADVISOR_LLM_SMOKE"

#: Names that would mean the transport, or a strategy holding one, can reach
#: something an advisor must not.  Design section 4.2: advisory components
#: "cannot add candidates, change targets, issue actuator commands, assign
#: verdicts, modify ledgers, release a target vector, or terminate a case",
#: and "have no direct actuator tools".
FORBIDDEN_REACH = (
    "actuator",
    "apply",
    "commit",
    "gateway",
    "kernel",
    "ledger",
    "permit",
    "rollback",
    "telnet",
    "trial",
)


class TheTransportSurfaceIsTextInTextOut(unittest.TestCase):
    def test_the_scripted_transport_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(scripted([]), AdvisoryTransport)

    def test_the_scripted_transport_adds_only_observation(self) -> None:
        # ``prompts`` and ``calls`` are what a test reads to prove the role
        # separation is real.  They are observation, not capability, and are
        # named here so the exception is deliberate rather than an oversight.
        public = {name for name in dir(ScriptedTransport([])) if not name.startswith("_")}
        self.assertEqual(public - ADVISORY_TRANSPORT_SURFACE, {"prompts", "calls"})

    def test_the_backend_adapter_exposes_only_the_surface(self) -> None:
        public = {
            name
            for name in dir(LLMBackendTransport)
            if not name.startswith("_")
        }
        self.assertEqual(public, set(ADVISORY_TRANSPORT_SURFACE))

    def test_no_transport_name_reaches_toward_control(self) -> None:
        for name in ADVISORY_TRANSPORT_SURFACE:
            lowered = name.lower()
            for forbidden in FORBIDDEN_REACH:
                with self.subTest(name=name, forbidden=forbidden):
                    self.assertNotIn(forbidden, lowered)

    def test_the_adapter_refuses_anything_that_is_not_a_backend(self) -> None:
        with self.assertRaises(TypeError):
            LLMBackendTransport(object())

    def test_a_completion_carries_only_text_and_accounting(self) -> None:
        completion = AdvisoryCompletion(
            text="{}",
            input_tokens=1,
            output_tokens=2,
            latency_ms=3.0,
            model_identity="mock",
            prompt_hash="h",
        )
        fields = set(vars(completion))
        self.assertEqual(
            fields,
            {
                "text",
                "input_tokens",
                "output_tokens",
                "latency_ms",
                "model_identity",
                "prompt_hash",
            },
        )
        self.assertEqual(completion.total_tokens, 3)

    def test_the_prompt_digest_is_a_real_content_hash(self) -> None:
        digest = prompt_digest(system_prompt="s", prompt="p")
        self.assertTrue(is_content_hash(digest))
        self.assertEqual(digest, prompt_digest(system_prompt="s", prompt="p"))
        self.assertNotEqual(digest, prompt_digest(system_prompt="s", prompt="q"))

    def test_a_scripted_failure_arrives_as_a_transport_error(self) -> None:
        with self.assertRaises(TransportError):
            scripted([]).complete(system_prompt="s", prompt="p")


class NoStrategyCanReachTheControlPath(unittest.TestCase):
    def _strategies(self):
        return (
            RoleSeparatedLLMCoordinator(transport=scripted(role_separated_script())),
            MonolithicLLMStrategy(transport=scripted([choice_reply()])),
        )

    def test_no_constructor_accepts_a_kernel_a_gateway_or_a_ledger(self) -> None:
        import inspect

        for cls in (RoleSeparatedLLMCoordinator, MonolithicLLMStrategy):
            parameters = set(inspect.signature(cls.__init__).parameters)
            # ``**kwargs`` forwards to the shared base, whose own parameters
            # are checked below, so both layers are covered.
            base_parameters = set(
                inspect.signature(cls.__mro__[1].__init__).parameters
            )
            for name in parameters | base_parameters:
                lowered = name.lower()
                for forbidden in FORBIDDEN_REACH:
                    with self.subTest(cls=cls.__name__, name=name, forbidden=forbidden):
                        self.assertNotIn(forbidden, lowered)

    def test_no_strategy_attribute_references_a_control_object(self) -> None:
        for strategy in self._strategies():
            for name, value in vars(strategy).items():
                lowered = name.lower()
                with self.subTest(strategy=strategy.strategy_id, attribute=name):
                    for forbidden in FORBIDDEN_REACH:
                        self.assertNotIn(forbidden, lowered)
                    self.assertNotIn("Gateway", type(value).__name__)
                    self.assertNotIn("Kernel", type(value).__name__)

    def test_a_strategy_exposes_no_method_that_applies_anything(self) -> None:
        allowed = {"propose", "describe", "telemetry", "meter", "fallbacks", "strategy_id",
                   "strategy_kind"}
        for strategy in self._strategies():
            public = {name for name in dir(strategy) if not name.startswith("_")}
            with self.subTest(strategy=strategy.strategy_id):
                self.assertEqual(public, allowed)

    def test_a_strategy_description_carries_no_credential(self) -> None:
        for strategy in self._strategies():
            described = strategy.describe()
            for name in described:
                with self.subTest(strategy=strategy.strategy_id, field=name):
                    self.assertFalse(is_credential_key(name))
            transport = described["transport"]
            for name in transport:
                with self.subTest(strategy=strategy.strategy_id, transportField=name):
                    self.assertFalse(is_credential_key(name))

    def test_a_run_makes_no_call_beyond_the_transport(self) -> None:
        # The whole strategy suite is hermetic: the only outbound interaction
        # a strategy has is ``complete``, and a scripted transport is where it
        # lands.
        transport = scripted(role_separated_script())
        RoleSeparatedLLMCoordinator(transport=transport).propose(**views())
        self.assertEqual(transport.calls, 3)


class TheBackendAdapterTranslatesFailuresHonestly(unittest.TestCase):
    """Built against a stand-in backend, so no provider is contacted."""

    def _backend(self, **overrides):
        from decision.llm_backend import LLMBackendBase, LLMResponse

        class StandInBackend(LLMBackendBase):
            api_key = "sk-must-never-be-described"

            def __init__(self, response):
                self._response = response
                self.calls = []

            @property
            def name(self) -> str:
                return "stand-in/model"

            def is_available(self) -> bool:
                return True

            def generate(self, prompt: str, system_prompt: str = "") -> LLMResponse:
                self.calls.append((system_prompt, prompt))
                if isinstance(self._response, BaseException):
                    raise self._response
                return self._response

        defaults = dict(success=True, content="{}", model="stand-in/model",
                        latency_ms=12.0, input_tokens=7, output_tokens=3)
        defaults.update(overrides)
        if isinstance(overrides.get("response"), BaseException):
            return StandInBackend(overrides["response"])
        return StandInBackend(LLMResponse(**defaults))

    def test_a_successful_response_becomes_a_completion(self) -> None:
        transport = LLMBackendTransport(self._backend())
        completion = transport.complete(system_prompt="s", prompt="p")
        self.assertEqual(completion.text, "{}")
        self.assertEqual(completion.total_tokens, 10)
        self.assertEqual(completion.latency_ms, 12.0)
        self.assertTrue(is_content_hash(completion.prompt_hash))

    def test_an_unsuccessful_response_becomes_a_transport_error(self) -> None:
        transport = LLMBackendTransport(self._backend(success=False, error="429"))
        with self.assertRaises(TransportError) as caught:
            transport.complete(system_prompt="s", prompt="p")
        self.assertIn("429", str(caught.exception))

    def test_an_empty_completion_becomes_a_transport_error(self) -> None:
        transport = LLMBackendTransport(self._backend(content="   "))
        with self.assertRaises(TransportError):
            transport.complete(system_prompt="s", prompt="p")

    def test_a_provider_exception_becomes_a_transport_error(self) -> None:
        transport = LLMBackendTransport(self._backend(response=RuntimeError("boom")))
        with self.assertRaises(TransportError):
            transport.complete(system_prompt="s", prompt="p")

    def test_the_description_is_a_whitelist_not_the_backends_dict(self) -> None:
        # Provider backends hold API keys as instance attributes; a
        # description assembled by reflection would put one in the run record.
        transport = LLMBackendTransport(self._backend())
        described = dict(transport.describe())
        self.assertEqual(set(described), {"transport", "transportId", "modelIdentity"})
        self.assertNotIn("sk-", str(described))

    def test_the_wrapped_backend_is_not_reachable_from_the_public_surface(self) -> None:
        transport = LLMBackendTransport(self._backend())
        public = {name for name in dir(transport) if not name.startswith("_")}
        self.assertTrue(public.issubset(ADVISORY_TRANSPORT_SURFACE), public)

    def test_an_unusable_backend_yields_no_transport_at_all(self) -> None:
        backend = self._backend()
        backend.is_available = lambda: False  # type: ignore[method-assign]
        self.assertIsNone(available_transport(backend))
        self.assertIsNone(available_transport(object()))


@unittest.skipUnless(
    os.environ.get(SMOKE_ENV) == "1",
    f"live provider smoke is opt-in: set {SMOKE_ENV}=1 (the suite is hermetic by default)",
)
class OneOptionalLiveSmoke(unittest.TestCase):
    """A single real call, only when a run explicitly asks for one.

    Not part of any gate.  It exists so that "the adapter works against a real
    provider" is checkable without the rest of the suite ever depending on a
    key, a network or a model's mood.  Skipped by default and skipped again if
    no backend reports itself available, so it can never fail for the absence
    of a credential.
    """

    def test_one_real_completion_comes_back_typed(self) -> None:
        from decision.llm_backend import LLMBackendManager

        manager = LLMBackendManager()
        backend = getattr(manager, "backend", None) or getattr(manager, "_backend", None)
        transport = available_transport(backend) if backend is not None else None
        if transport is None:
            self.skipTest("no configured LLM backend reports itself available")

        strategy = MonolithicLLMStrategy(transport=transport)
        message = strategy.propose(**views())
        self.assertIsNotNone(message)
        # Either the model answered inside the schema, or the deterministic
        # fallback did its job.  Both are passes: the claim is that a real
        # provider cannot produce an untyped or unbounded result.
        telemetry = strategy.telemetry()
        self.assertEqual(telemetry["totals"]["calls"], 1)
        self.assertLessEqual(
            telemetry["totals"]["tokens"], strategy.meter.budget.max_total_tokens
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
