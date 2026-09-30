"""Backend usage capture: the provider's own envelope, counts and served id.

Every SDK response here is a local stand-in -- no API key, no network, no
model call.  The drop asks for the ACTUAL response envelope, so each fake
carries the field names its provider really uses, and one Gemini case builds
the genuine ``google.generativeai`` protobuf so the shape this code reads is
checked against the shape the installed library returns.

One rule under test everywhere: a count the provider did NOT report is
``None`` (unknown), never 0 (a claim that the call was free), and the served
model is read off the response body, never copied from the request label.
"""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from decision.llm_backend import (ClaudeBackend, GeminiBackend, LiteLLMBackend,
                                  OllamaBackend, OpenAIBackend, reasoning_count,
                                  reported_count)

REQUESTED = "requested-route"


def backend(cls, model=REQUESTED):
    """A backend with its transport replaced; __init__ (and its key read) skipped."""
    obj = cls.__new__(cls)
    obj.model, obj.api_key, obj.client = model, "fake", Mock()
    obj.base_url, obj.max_tokens, obj.temperature, obj.timeout = (
        "http://unused", 4096, 0.3, 300.0)
    obj._available = True
    return obj


class TheCountHelperNeverInventsANumber(unittest.TestCase):
    def test_only_a_real_integer_is_a_count(self):
        self.assertEqual(7, reported_count({"a": 7}, "a"))
        # A provider that REPORTED zero measured zero; that one survives.
        self.assertEqual(0, reported_count({"a": 0}, "a"))
        self.assertIsNone(reported_count(None, "a"))
        self.assertIsNone(reported_count({}, "a"))
        for junk in (None, "12", True, 1.5, [3]):
            with self.subTest(value=junk):
                self.assertIsNone(reported_count({"a": junk}, "a"))

    def test_the_first_name_actually_present_wins(self):
        self.assertEqual(5, reported_count({"b": 5}, "a", "b"))
        self.assertEqual(9, reported_count({"a": 9, "b": 5}, "a", "b"))


class TheReasoningHelperReadsWhereProvidersHideIt(unittest.TestCase):
    """Every provider nests its reasoning count differently, one level down."""

    def test_each_provider_nesting_is_found(self):
        # Anthropic-shaped (what the proxy in front of this lab returns)
        self.assertEqual(2407, reasoning_count(
            {"output_tokens_details": {"thinking_tokens": 2407}}))
        # OpenAI-shaped
        self.assertEqual(5, reasoning_count(
            {"completion_tokens_details": {"reasoning_tokens": 5}}))
        # Gemini-shaped, flat
        self.assertEqual(11, reasoning_count({"thoughts_token_count": 11}))
        # A provider that REPORTED zero measured zero; that one survives.
        self.assertEqual(0, reasoning_count(
            {"output_tokens_details": {"thinking_tokens": 0}}))

    def test_an_unreported_count_is_none_never_zero(self):
        for absent in (None, {}, {"output_tokens_details": None},
                       {"output_tokens_details": {}},
                       {"output_tokens_details": {"thinking_tokens": "2407"}},
                       {"input_tokens": 10, "output_tokens": 2}):
            with self.subTest(usage=absent):
                self.assertIsNone(reasoning_count(absent))


class ClaudeShapedProxy(unittest.TestCase):
    """The Anthropic-shaped proxy bills reasoning inside ``output_tokens`` and
    itemizes it under ``output_tokens_details``."""

    def respond(self, obj, usage, **extra):
        obj.client.messages.create.return_value = NS(
            content=[NS(type="text", text="{}")], usage=usage, **extra)

    def test_reported_reasoning_tokens_are_carried(self):
        obj = backend(ClaudeBackend)
        self.respond(obj, NS(input_tokens=3148, output_tokens=3728,
                             output_tokens_details={"thinking_tokens": 2407}),
                     model="gpt-5.6-luna")
        r = obj.generate("p")
        self.assertTrue(r.success, r.error)
        self.assertEqual(2407, r.reasoning_tokens)
        # Already inside the output bill, not added to it.
        self.assertEqual(3728, r.output_tokens)
        self.assertEqual({"thinking_tokens": 2407}, r.usage["output_tokens_details"])
        self.assertEqual("gpt-5.6-luna", r.response_model)
        self.assertEqual(REQUESTED, r.requested_model)

    def test_a_provider_reporting_no_reasoning_leaves_it_unknown(self):
        obj = backend(ClaudeBackend)
        self.respond(obj, NS(input_tokens=10, output_tokens=2))
        r = obj.generate("p")
        self.assertTrue(r.success, r.error)
        self.assertIsNone(r.reasoning_tokens)
        self.assertEqual(2, r.output_tokens)


class OpenAIShapedBackends(unittest.TestCase):
    """OpenAI and the LiteLLM proxy both return an OpenAI chat completion."""

    def chat(self, obj, usage, **extra):
        obj.client.chat.completions.create.return_value = NS(
            choices=[NS(message=NS(content="{}"))], usage=usage, **extra)

    def test_the_whole_envelope_survives_the_boundary(self):
        for cls in (OpenAIBackend, LiteLLMBackend):
            with self.subTest(backend=cls.__name__):
                obj = backend(cls)
                self.chat(obj, NS(prompt_tokens=31, completion_tokens=7,
                                  total_tokens=38,
                                  prompt_tokens_details={"cached_tokens": 16},
                                  completion_tokens_details={"reasoning_tokens": 5}),
                          model="served-not-requested")
                r = obj.generate("p")
                self.assertTrue(r.success, r.error)
                self.assertEqual(r.input_tokens, 31)
                self.assertEqual(r.output_tokens, 7)
                self.assertEqual(r.usage["total_tokens"], 38)
                # cached input and reasoning tokens are not thrown away
                self.assertEqual(r.usage["prompt_tokens_details"], {"cached_tokens": 16})
                self.assertEqual(r.usage["completion_tokens_details"],
                                 {"reasoning_tokens": 5})
                # ...and the reasoning count is now read as a named field.
                self.assertEqual(5, r.reasoning_tokens)
                self.assertEqual(r.requested_model, REQUESTED)
                self.assertEqual(r.response_model, "served-not-requested")

    def test_a_response_without_usage_is_unknown_not_zero(self):
        for cls in (OpenAIBackend, LiteLLMBackend):
            with self.subTest(backend=cls.__name__):
                obj = backend(cls)
                self.chat(obj, None)
                r = obj.generate("p")
                # OpenAI used to raise AttributeError here and lose the answer.
                self.assertTrue(r.success, r.error)
                self.assertIsNone(r.usage)
                self.assertIsNone(r.input_tokens)
                self.assertIsNone(r.output_tokens)
                self.assertIsNone(r.response_model)
                self.assertEqual(r.requested_model, REQUESTED)


class Ollama(unittest.TestCase):
    """Ollama reports its counters flat in the body, with no usage object."""

    def post(self, body):
        posted = Mock(status_code=200)
        posted.json.return_value = body
        return patch("requests.post", return_value=posted)

    def test_the_flat_counters_and_timings_are_kept(self):
        obj = backend(OllamaBackend, "qwen3")
        with self.post({"response": "{}", "model": "qwen3:32b", "done": True,
                        "prompt_eval_count": 412, "eval_count": 96,
                        "prompt_eval_duration": 1_200_000, "eval_duration": 8_400_000,
                        "total_duration": 9_900_000, "load_duration": 300_000,
                        "context": [1, 2, 3]}):
            r = obj.generate("p")
        self.assertTrue(r.success, r.error)
        self.assertEqual(r.input_tokens, 412)
        self.assertEqual(r.output_tokens, 96)
        self.assertEqual(r.usage["eval_duration"], 8_400_000)
        self.assertEqual(r.usage["total_duration"], 9_900_000)
        self.assertEqual(r.usage["load_duration"], 300_000)
        # the answer and the token context are not usage
        self.assertNotIn("response", r.usage)
        self.assertNotIn("context", r.usage)
        self.assertEqual(r.requested_model, "qwen3")
        self.assertEqual(r.response_model, "qwen3:32b")

    def test_a_body_without_counters_is_unknown_not_zero(self):
        obj = backend(OllamaBackend, "qwen3")
        with self.post({"response": "{}"}):
            r = obj.generate("p")
        self.assertTrue(r.success, r.error)
        self.assertIsNone(r.usage)
        self.assertIsNone(r.input_tokens)
        self.assertIsNone(r.output_tokens)
        self.assertIsNone(r.response_model)
        self.assertEqual(r.requested_model, "qwen3")


class Gemini(unittest.TestCase):
    def test_usage_metadata_and_model_version_are_read(self):
        obj = backend(GeminiBackend, "gemini-1.5-pro")
        obj.client.generate_content.return_value = NS(
            text="{}", model_version="gemini-1.5-pro-002",
            usage_metadata=NS(prompt_token_count=88, candidates_token_count=12,
                              cached_content_token_count=40, total_token_count=100))
        r = obj.generate("p")
        self.assertTrue(r.success, r.error)
        self.assertEqual(r.input_tokens, 88)
        self.assertEqual(r.output_tokens, 12)
        self.assertEqual(r.usage["cached_content_token_count"], 40)
        self.assertEqual(r.usage["total_token_count"], 100)
        self.assertEqual(r.requested_model, "gemini-1.5-pro")
        # off model_version, never off the request label
        self.assertEqual(r.response_model, "gemini-1.5-pro-002")

    def test_no_usage_metadata_is_unknown_not_the_old_hardcoded_zero(self):
        obj = backend(GeminiBackend, "gemini-1.5-pro")
        obj.client.generate_content.return_value = NS(text="{}")
        r = obj.generate("p")
        self.assertTrue(r.success, r.error)
        self.assertIsNone(r.usage)
        self.assertIsNone(r.input_tokens)
        self.assertIsNone(r.output_tokens)
        self.assertIsNone(r.response_model)
        self.assertEqual(r.requested_model, "gemini-1.5-pro")

    def test_the_envelope_matches_the_installed_library_shape(self):
        """The genuine protobuf, not a hand-written stand-in: the fields read
        here have to be the fields google-generativeai actually returns."""
        try:
            from google.ai.generativelanguage_v1beta.types import GenerateContentResponse
        except Exception as exc:                    # pragma: no cover - optional dep
            self.skipTest(f"google-generativeai not importable: {exc}")
        meta = GenerateContentResponse.UsageMetadata(
            prompt_token_count=88, candidates_token_count=12,
            cached_content_token_count=40, total_token_count=100)
        obj = backend(GeminiBackend, "gemini-1.5-pro")
        obj.client.generate_content.return_value = NS(
            text="{}", usage_metadata=meta, model_version="gemini-1.5-pro-002")
        r = obj.generate("p")
        self.assertTrue(r.success, r.error)
        self.assertEqual(r.input_tokens, 88)
        self.assertEqual(r.output_tokens, 12)
        self.assertEqual(r.usage["cached_content_token_count"], 40)
        self.assertEqual(r.usage["total_token_count"], 100)


if __name__ == "__main__":
    unittest.main()
