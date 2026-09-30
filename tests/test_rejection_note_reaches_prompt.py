"""A decision the sitting refused is asked again with the refusal in the prompt (2026-09-30,
v5.4t: the re-ask was byte-identical and qwen3 returned the same tried configuration 57-98
times per board)."""
import unittest

from assurance.coordination.agents import PHASE_SELECTION, ROLE_MONOLITH, RoleAgents, RoleModels


class RejectionNote(unittest.TestCase):
    def _prompt(self, note):
        agents = RoleAgents(models=RoleModels())
        agents.rejection_note = note
        _value, record = agents._decide(ROLE_MONOLITH, PHASE_SELECTION, "system", {"input.x": 1},
                                        {}, lambda parsed, rec: parsed, lambda: None)
        return record.prompt

    def test_note_is_appended(self):
        prompt = self._prompt("M11 has already been tried; the frozen catalog admits each candidate once")
        self.assertIn("Your previous answer was refused: M11 has already been tried", prompt)
        self.assertIn("Choose a different answer", prompt)

    def test_no_note_leaves_the_prompt_alone(self):
        self.assertNotIn("previous answer was refused", self._prompt(""))


if __name__ == "__main__":
    unittest.main()
