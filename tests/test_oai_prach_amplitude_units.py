"""Units and patch-scope regressions, not an execution of the OAI PHY.

These stdlib-only checks read only the in-repo patch. They do not load/build
OAI, access equipment, or establish any OTA success or calibrated RF power.
"""
from pathlib import Path
import re
import unittest


PATCH = (Path(__file__).resolve().parents[1]
         / "oai_patches" / "nr_prach_amplitude_units.w30.patch")


def dbm_to_milliwatts(power_dbm):
    return 10 ** (power_dbm / 10)


class PrachAmplitudeUnitsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patch = PATCH.read_text()
        cls.added = [line[1:] for line in cls.patch.splitlines()
                     if line.startswith("+") and not line.startswith("+++")]
        cls.removed = [line[1:] for line in cls.patch.splitlines()
                       if line.startswith("-") and not line.startswith("---")]

    def test_one_line_uses_prepared_linear_amplitude(self):
        self.assertEqual(self.added,
                         ["  const int16_t amp = ue->prach_vars[gNB_id]->amp;"])

    def test_one_line_removes_logarithmic_power_as_amplitude(self):
        self.assertEqual(self.removed,
                         ["  const int16_t amp = prach_pdu->prach_tx_power;"])

    def test_only_prach_generator_is_modified(self):
        paths = re.findall(r"^\+\+\+ b/(.+)$", self.patch, re.MULTILINE)
        self.assertEqual(paths, ["openair1/PHY/NR_UE_TRANSPORT/nr_prach.c"])
        self.assertEqual(len(re.findall(r"^@@ ", self.patch, re.MULTILINE)), 1)

    def test_zero_dbm_is_not_zero_signal_power(self):
        self.assertEqual(dbm_to_milliwatts(0), 1.0)
        self.assertNotEqual(dbm_to_milliwatts(0), 0 ** 2)

    def test_negative_dbm_still_represents_positive_power(self):
        for dbm in (-96, -84, -60, -20, -1):
            with self.subTest(dbm=dbm):
                self.assertGreater(dbm_to_milliwatts(dbm), 0)

    def test_ramping_dbm_cannot_be_mapped_by_squaring_its_numeric_value(self):
        low, high = -84, -60
        self.assertGreater(dbm_to_milliwatts(high), dbm_to_milliwatts(low))
        self.assertLess(high ** 2, low ** 2)

    def test_logarithmic_change_requires_exponential_amplitude_ratio(self):
        low, high = -84, -60
        voltage_ratio = 10 ** ((high - low) / 20)
        power_ratio = dbm_to_milliwatts(high) / dbm_to_milliwatts(low)
        self.assertAlmostEqual(voltage_ratio ** 2, power_ratio)
        self.assertNotAlmostEqual(abs(high / low), voltage_ratio)


if __name__ == "__main__":
    unittest.main()
