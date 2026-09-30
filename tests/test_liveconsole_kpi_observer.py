"""What the radio can be asked for, and what it must refuse to be asked for.

Scenario I4's ``deadlineSuccessRatio`` is the first KPI this build can emulate
and predict but **cannot observe**: no tagged-echo traffic generator runs on
this deployment, so nothing writes the timestamped echo log a round trip would
be measured from.  Two things are asserted here.

* :class:`TaggedEchoObserver` is the shape that log is read in, exercised
  against an injected reader so no test ever opens ssh: the denominator is
  every eligible issued request, the ones that never answered included, and a
  request whose deadline has not passed yet is neither a success nor a miss.
* the live composition **refuses the KPI by name**, with the reason, exactly
  as ``tools/liveconsole/agent.py`` refuses an action axis nothing on this wire
  can read back -- and never substitutes the emulator for it.

Hermetic: the runner is a fake, the clock is a number.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from tools.liveconsole.kpi_observer import (
    DEADLINE_RATIO_KPI, GOODPUT_KPI, LIVE_UNOBSERVED_KPIS, KpiObserverError,
    TaggedEchoObserver, TunRateObserver, build_live_observer,
    refuse_unobserved_kpis,
)


class _Runner:
    """``subprocess``'s shape, answering from a table instead of a host."""

    def __init__(self, stdout_by_host, returncode=0):
        self.stdout_by_host, self.returncode = dict(stdout_by_host), returncode
        self.commands = []

    def run(self, argv, **_kwargs):
        self.commands.append(list(argv))
        alias = argv[-2]
        if alias not in self.stdout_by_host:
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "no host"})()
        return type("R", (), {"returncode": self.returncode,
                              "stdout": self.stdout_by_host[alias],
                              "stderr": ""})()


def echo_log(rows):
    return "\n".join(json.dumps(row) for row in rows) + "\n"


class TheTaggedEchoObserver(unittest.TestCase):
    """One log line per request; the misses are the point of reading it."""

    LOG = echo_log([
        {"seq": 1, "issuedAtMs": 0, "rttMs": 12.0},
        {"seq": 2, "issuedAtMs": 100, "rttMs": 48.5},
        {"seq": 3, "issuedAtMs": 200, "rttMs": 51.0},      # answered, too late
        {"seq": 4, "issuedAtMs": 300, "rttMs": None},      # never answered
        {"seq": 5, "issuedAtMs": 400},                     # never answered
    ])

    def observer(self, **kwargs):
        kwargs.setdefault("hosts", {"131": "ue1"})
        kwargs.setdefault("deadline_ms", 50.0)
        kwargs.setdefault("monotonic_ms", lambda: 10000.0)
        kwargs.setdefault("runner", _Runner({"ue1": self.LOG}))
        return TaggedEchoObserver(**kwargs)

    def test_every_eligible_issued_request_is_in_the_denominator(self):
        counters = self.observer().sample()[f"{DEADLINE_RATIO_KPI}@131"]
        self.assertEqual({"issued": 5, "eligible": 5, "completed": 2}, counters)

    def test_a_request_whose_deadline_has_not_passed_is_not_yet_eligible(self):
        observer = self.observer(monotonic_ms=lambda: 260.0)
        counters = observer.sample()[f"{DEADLINE_RATIO_KPI}@131"]
        # issued at 0, 100 and 200 are past their 50 ms; 300 and 400 are not
        self.assertEqual({"issued": 5, "eligible": 3, "completed": 2}, counters)

    def test_a_looser_deadline_completes_more_of_the_same_log(self):
        self.assertEqual(
            3,
            self.observer(deadline_ms=60.0).sample()[
                f"{DEADLINE_RATIO_KPI}@131"]["completed"])

    def test_a_repeated_sequence_number_is_one_request(self):
        log = echo_log([{"seq": 9, "issuedAtMs": 0, "rttMs": 5.0},
                        {"seq": 9, "issuedAtMs": 0, "rttMs": 5.0}])
        counters = self.observer(runner=_Runner({"ue1": log})).sample()[
            f"{DEADLINE_RATIO_KPI}@131"]
        self.assertEqual({"issued": 1, "eligible": 1, "completed": 1}, counters)

    def test_an_issue_and_its_reply_are_one_request_with_a_round_trip(self):
        # The generator's own format: the reply is a separate record sharing the
        # sequence number. Keeping only the first record loses every rttMs and
        # makes completed structurally zero.
        log = echo_log([
            {"event": "start", "atMs": 0},
            {"event": "heartbeat", "atMs": 0, "status": "running"},
            {"event": "issued", "atMs": 0, "seq": 0, "issuedAtMs": 0},
            {"event": "reply", "atMs": 12, "seq": 0, "rttMs": 12.0},
            {"event": "issued", "atMs": 100, "seq": 1, "issuedAtMs": 100},
            {"event": "reply", "atMs": 160, "seq": 1, "rttMs": 60.0},   # too late
            {"event": "issued", "atMs": 200, "seq": 2, "issuedAtMs": 200},
            {"event": "send-error", "atMs": 200, "seq": 2, "error": "no route"},
            {"event": "end", "atMs": 300, "status": "finished"},
        ])
        counters = self.observer(runner=_Runner({"ue1": log})).sample()[
            f"{DEADLINE_RATIO_KPI}@131"]
        self.assertEqual({"issued": 3, "eligible": 3, "completed": 1}, counters)

    def test_start_heartbeat_and_end_markers_are_not_failed_requests(self):
        log = echo_log([{"event": "start", "atMs": 0},
                        {"event": "heartbeat", "atMs": 0, "status": "running",
                         "interface": {"name": "oaitun_ue1", "up": True}},
                        {"event": "end", "atMs": 300, "status": "finished"}])
        counters = self.observer(runner=_Runner({"ue1": log})).sample()[
            f"{DEADLINE_RATIO_KPI}@131"]
        self.assertEqual({"issued": 0, "eligible": 0, "completed": 0}, counters)

    def test_a_line_it_cannot_read_is_skipped_not_counted_as_a_miss(self):
        log = "not json\n" + echo_log([{"seq": 1, "issuedAtMs": 0, "rttMs": 5.0}])
        counters = self.observer(runner=_Runner({"ue1": log})).sample()[
            f"{DEADLINE_RATIO_KPI}@131"]
        self.assertEqual({"issued": 1, "eligible": 1, "completed": 1}, counters)

    def test_a_ue_that_could_not_be_read_contributes_no_key(self):
        observer = self.observer(hosts={"131": "ue1", "132": "ue2"})
        sample = observer.sample()
        self.assertEqual([f"{DEADLINE_RATIO_KPI}@131"], list(sample))
        self.assertEqual(["132"], [row["ueId"] for row in observer.failures])

    def test_it_is_composed_the_way_the_tun_observer_is(self):
        observer = self.observer()
        observer.sample()
        command = observer.runner.commands[0]
        self.assertEqual(command[:3], ["ssh", "-o", "BatchMode=yes"])
        self.assertEqual(command[-2:], ["ue1", "cat /tmp/tagged-echo.jsonl"])

    def test_a_ratio_with_no_deadline_to_be_within_is_refused(self):
        with self.assertRaisesRegex(KpiObserverError, "no deadline"):
            self.observer(deadline_ms=0.0).sample()


class TheLiveRefusal(unittest.TestCase):
    """No generator, no KPI -- and the refusal says which and why."""

    def test_the_deadline_ratio_is_refused_by_name_with_the_reason(self):
        with self.assertRaises(KpiObserverError) as caught:
            build_live_observer([f"{GOODPUT_KPI}@131", f"{DEADLINE_RATIO_KPI}@131"],
                                hosts={"131": "ue1"})
        message = str(caught.exception)
        self.assertIn(f"{DEADLINE_RATIO_KPI}@131", message)
        self.assertIn("no tagged-echo traffic generator", message)
        self.assertIn(LIVE_UNOBSERVED_KPIS[DEADLINE_RATIO_KPI], message)

    def test_the_refusal_names_both_ways_forward(self):
        with self.assertRaises(KpiObserverError) as caught:
            refuse_unobserved_kpis([f"{DEADLINE_RATIO_KPI}@131"])
        self.assertIn("Drop the intent", str(caught.exception))
        self.assertIn(f"KPI observer for {DEADLINE_RATIO_KPI}",
                      str(caught.exception))

    def test_an_observable_kpi_composes_the_observer_it_always_did(self):
        observer = build_live_observer([f"{GOODPUT_KPI}@131"],
                                       hosts={"131": "ue1"})
        self.assertIsInstance(observer, TunRateObserver)
        self.assertEqual({"131": "ue1"}, dict(observer.hosts))

    def test_an_injected_observer_answers_for_its_own_kind(self):
        # what makes the hardware-free runtime the same composition with
        # different ports -- and not a licence to inject the emulator live
        source = type('Source', (), {'sample': lambda self: {
            f'{DEADLINE_RATIO_KPI}@131': {'eligible': 3, 'completed': 2}}})()
        observer = build_live_observer(
            [f"{DEADLINE_RATIO_KPI}@131"], hosts={},
            observers={DEADLINE_RATIO_KPI: source})
        self.assertEqual(source.sample(), observer.sample())
        with self.assertRaisesRegex(KpiObserverError, 'sample'):
            build_live_observer([f"{DEADLINE_RATIO_KPI}@131"], hosts={},
                                observers={DEADLINE_RATIO_KPI: object()})

    def test_nothing_here_imports_the_emulator(self):
        import tools.liveconsole.kpi_observer as module
        self.assertNotIn("hfconsole", module.__file__)
        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("hfconsole", source.split('"""', 2)[2])


if __name__ == "__main__":
    unittest.main()

class SshFailureSaysWhy(unittest.TestCase):
    """실패 문자열이 원인을 가리면 안 된다 (2026-09-18).

    `error` 가 "ssh exit 1" 뿐이라 읽는 사람을 전송 문제로 보낸다.  이유는 한 칸
    옆 `stderr` 에 이미 있었다 — 판 20260918T100431 의 ue1 실패 70건이 전부
    "flow-goodput: no complete running heartbeat" 였고, 그건 ue1 이 끊겨 부하원이
    돌지 않았다는 뜻이다.  종료코드는 앞에 남겨 기존 매칭을 깨지 않는다.
    """

    def failure(self, rc, stderr):
        from tools.liveconsole.kpi_observer import _ssh_failure
        return _ssh_failure('ue1', 'ue1', SimpleNamespace(returncode=rc, stderr=stderr))

    def test_the_reason_is_carried_in_the_error_itself(self):
        row = self.failure(1, 'flow-goodput: no complete running heartbeat\n')
        self.assertEqual(row['error'],
                         'ssh exit 1: flow-goodput: no complete running heartbeat')
        self.assertIn('ssh exit', row['error'])      # 기존 집계가 계속 동작한다

    def test_no_stderr_leaves_the_bare_exit_code(self):
        self.assertEqual(self.failure(255, '')['error'], 'ssh exit 255')

    def test_only_the_first_non_blank_line_is_used(self):
        self.assertEqual(self.failure(1, '\n\n  진짜 이유  \n두번째줄\n')['error'],
                         'ssh exit 1: 진짜 이유')

