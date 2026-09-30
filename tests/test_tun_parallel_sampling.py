"""Concurrent counter acquisition without hardware or timing-sensitive sleeps."""
import threading
import unittest
from types import SimpleNamespace
from tools.liveconsole.kpi_observer import TunRateObserver


class ParallelTunSampling(unittest.TestCase):
    def test_independent_hosts_are_read_together_and_failures_stay_missing(self):
        barrier = threading.Barrier(3, timeout=3)
        counters = {'ue1': 100, 'ue2': 200, 'ue3': 300}
        failures = set()
        def run(argv, **kwargs):
            host = argv[-2]
            barrier.wait()  # Serial reads would fail here, without real I/O.
            if host in failures:
                raise TimeoutError('unreachable')
            return SimpleNamespace(returncode=0, stdout=str(counters[host]))
        clock = [0.0]
        observer = TunRateObserver(hosts={'1': 'ue1', '2': 'ue2', '3': 'ue3'},
            runner=SimpleNamespace(run=run), monotonic_ms=lambda: clock[0])
        self.assertEqual({}, observer.sample())
        clock[0] = 1000.0
        counters.update(ue1=50100, ue2=62700)
        failures.add('ue3')
        self.assertEqual({'dlGoodputMbps@1': 0.4, 'dlGoodputMbps@2': 0.5}, observer.sample())
        self.assertEqual('3', observer.failures[0]['ueId'])
