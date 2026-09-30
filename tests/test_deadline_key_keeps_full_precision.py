"""2026-09-26 block 15: d = 1886.495219051838 ms went over the wire as 12 significant digits
(1886.49521905, 1.8e-9 off) and the target judge, which matches within 1e-9, found no ratio --
every I2d of the block's first board came out UNKNOWN on valid counts.  The observer now reports
under the configured deadline when the wire form does not round-trip."""
import unittest

from assurance.coordination.tc import observation_at_deadline

D = 1886.495219051838


def _key(deadline):
    # the rule tools/liveconsole/kpi_observer.py applies to each configured deadline
    wire = format(deadline, '.12g')
    return wire if float(wire) == float(deadline) else repr(float(deadline))


class DeadlineKey(unittest.TestCase):
    def test_the_wire_form_alone_is_not_found(self):
        self.assertIsNone(observation_at_deadline({'byDeadlineMs': {format(D, '.12g'): 1.0}}, D))

    def test_the_reported_key_is_found(self):
        self.assertEqual(1.0, observation_at_deadline({'byDeadlineMs': {_key(D): 1.0}}, D))

    def test_round_deadlines_keep_their_short_key(self):
        self.assertEqual('3000', _key(3000.0))
        self.assertEqual('52.6471870542', _key(52.6471870542))

    def test_the_observer_applies_this_rule(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / 'tools/liveconsole/kpi_observer.py').read_text()
        self.assertIn("key = wire if float(wire) == float(deadline) else repr(float(deadline))", src)


if __name__ == '__main__':
    unittest.main()
