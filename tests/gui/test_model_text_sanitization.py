import unittest

from gui.operator.sources.live import project_decision
from gui.operator.widgets.modeltext import (
    MODEL_TEXT_LIMIT, SUPPRESSED_LABEL, TRUNCATED_LABEL, sanitize_model_text,
)


class ModelTextSanitizationTest(unittest.TestCase):
    def test_thinking_block_is_suppressed(self):
        value = "visible summary <think>private step by step material</think> final"
        safe = sanitize_model_text(value)
        self.assertNotIn("private step", safe.text)
        self.assertTrue(safe.suppressed)
        self.assertIn(SUPPRESSED_LABEL, safe.marker)

    def test_private_reasoning_variants_are_never_visible(self):
        cases = (
            ("<thinking>thinking-secret</thinking> visible", "thinking-secret"),
            ("<scratchpad>scratch-secret</scratchpad> visible", "scratch-secret"),
            ("<|channel|>analysis channel-secret", "channel-secret"),
            ("Thought: thought-secret\nVisible summary", "thought-secret"),
            ('{"reasoning": "json-secret", "answer": "visible"}', "json-secret"),
        )
        for value, secret in cases:
            with self.subTest(value=value):
                safe = sanitize_model_text(value)
                self.assertNotIn(secret, safe.text)
                self.assertTrue(safe.suppressed)
                self.assertIn(SUPPRESSED_LABEL, safe.marker)

    def test_long_reasoning_is_bounded_and_marked(self):
        safe = sanitize_model_text("reason " * 500)
        self.assertLessEqual(len(safe.text), MODEL_TEXT_LIMIT)
        self.assertTrue(safe.truncated)
        self.assertIn(TRUNCATED_LABEL, safe.marker)

    def test_schema_rejection_model_output_uses_same_policy(self):
        result = {
            "terminal_outcome": "technical_failsafe",
            "schema_reject_reason": (
                'bad response, reasoning_content="private hidden analysis" ' + "x" * 900),
        }
        decision = project_decision(result)
        self.assertNotIn("private hidden analysis", decision.reasoning_summary)
        self.assertTrue(decision.reasoning_truncated)
        self.assertEqual(decision.eq12_state, "TechnicalFailsafe")


if __name__ == "__main__":
    unittest.main()
