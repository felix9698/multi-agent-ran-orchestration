"""Which model actually served a call, per backend.

The proxy this lab talks to advertises obfuscated ids and its access log
carries no model identifier at all, so served-model identity cannot be
recovered after the fact -- it has to be read off the response body at the
boundary and kept.  ``None`` means unknown and must never be rendered as the
requested label, or a fallback reads back as a normal run.

Provider SDK calls are mocked: no API keys, no network.
"""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from decision.llm_backend import (LiteLLMBackend, OllamaBackend, OpenAIBackend,
                                  served_model)


class TheReportedValueIsNormalized(unittest.TestCase):
    def test_only_a_non_empty_string_is_an_identity(self):
        self.assertEqual("gpt-5.6-luna", served_model("  gpt-5.6-luna  "))
        for unknown in (None, "", "   ", Mock(), 7, b"gpt", {"model": "x"}):
            with self.subTest(reported=unknown):
                self.assertIsNone(served_model(unknown))


class BackendsCarryTheServedModel(unittest.TestCase):
    """One case where the provider reports an id, one where it does not."""

    def backend(self, cls, model="requested-route"):
        obj = cls.__new__(cls)
        obj.model, obj.api_key, obj.client = model, "fake", Mock()
        obj.base_url, obj.max_tokens, obj.temperature, obj.timeout = (
            "http://unused", 4096, 0.3, 300.0)
        obj._available = True
        return obj

    def chat(self, obj, **extra):
        obj.client.chat.completions.create.return_value = NS(
            choices=[NS(message=NS(content="{}"))],
            usage=NS(prompt_tokens=10, completion_tokens=2), **extra)

    def test_openai_shaped_backends_read_it_off_the_response(self):
        for cls in (OpenAIBackend, LiteLLMBackend):
            with self.subTest(backend=cls.__name__):
                obj = self.backend(cls)
                self.chat(obj, model="gpt-served-test")
                response = obj.generate("p")
                self.assertTrue(response.success, response.error)
                self.assertEqual(response.requested_model, "requested-route")
                self.assertEqual(response.response_model, "gpt-served-test")
                # The legacy label keeps meaning "what we asked for".
                self.assertEqual(response.model, "requested-route")

    def test_a_response_that_reports_nothing_stays_unknown(self):
        for cls in (OpenAIBackend, LiteLLMBackend):
            for reported in ({}, {"model": None}, {"model": "  "}):
                with self.subTest(backend=cls.__name__, reported=reported):
                    obj = self.backend(cls)
                    self.chat(obj, **reported)
                    response = obj.generate("p")
                    self.assertTrue(response.success, response.error)
                    self.assertEqual(response.requested_model, "requested-route")
                    # Not the alias: unknown is unknown.
                    self.assertIsNone(response.response_model)

    def test_ollama_reads_the_model_its_body_names(self):
        for reported, expected in (({"model": "qwen3:32b"}, "qwen3:32b"), ({}, None)):
            with self.subTest(reported=reported):
                obj = self.backend(OllamaBackend, "qwen3")
                obj.session = Mock()
                body = {"response": "{}", "prompt_eval_count": 3, "eval_count": 1}
                body.update(reported)
                posted = Mock(status_code=200)
                posted.json.return_value = body
                with unittest.mock.patch("requests.post", return_value=posted):
                    response = obj.generate("p")
                self.assertTrue(response.success, response.error)
                self.assertEqual(response.requested_model, "qwen3")
                self.assertEqual(response.response_model, expected)


if __name__ == "__main__":
    unittest.main()
