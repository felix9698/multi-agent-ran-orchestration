"""2026-09-30 (owner): AIC_FAIR_PROMPTS=1 gives Control and the basic monolith the same energy sentence and
drops Control's fixed slot quota; unset keeps the v5.3 texts."""
import os
import subprocess
import sys
import unittest

CODE = ("from assurance.coordination import v52; S = v52.SYSTEM_V53; "
        "print(int('any supported attenuation' in S['control']), int('any supported attenuation' in S['basic-monolith']), "
        "int('any supported attenuation' in S['monolith-form']), int('four candidates' in S['control']))")


def texts(fair):
    env = dict(os.environ, AIC_FAIR_PROMPTS="1" if fair else "")
    return subprocess.run([sys.executable, "-c", CODE], env=env, capture_output=True, text=True,
                          cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))).stdout.split()


class FairPrompts(unittest.TestCase):
    def test_fair_texts_only_when_asked(self):
        self.assertEqual(texts(False), ["0", "0", "0", "1"])
        self.assertEqual(texts(True), ["1", "1", "1", "0"])


if __name__ == "__main__":
    unittest.main()
