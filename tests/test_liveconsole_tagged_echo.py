"""Hermetic tagged-echo source tests: no real sockets, not even loopback.

The fake select advances a UE-local clock. Source logs are real temporary files;
network traffic, interface ioctls and boot identities are injected or mocked.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools.liveconsole import tagged_echo as te


IDENTITY = {"name": "oaitun_ue1", "ip": "12.1.1.2", "ifindex": 7, "up": True}
COMMON = {"schemaVersion": te.LOG_SCHEMA, "sessionId": "test-session",
          "flowId": "test-flow", "clockId": "test-boot"}


def record(event, at, **fields):
    return dict(COMMON, event=event, atMs=at, **fields)


def start():
    return record("start", 0, status="starting", interfaceName=te.INTERFACE)


def heartbeat(at, **fields):
    return record("heartbeat", at, **dict({"status": "running", "interface": dict(IDENTITY)}, **fields))


def issue(seq, at):
    return record("issued", at, seq=seq, issuedAtMs=at)


def reply(seq, at, rtt):
    return record("reply", at, seq=seq, rttMs=rtt)


class FakeNetwork:
    """A socket and select fake; time advances only while waiting for I/O."""

    def __init__(self, log_path=None):
        self.now = 0.0
        self.log_path = log_path
        self.queue = []
        self.sent = []
        self.options = []
        self.binds = []
        self.connected = None
        self.blocking = None
        self.closed = False
        self.calls = 0
        self.echo_delay = {}
        self.send_errors = set()
        self.option_error = None
        self.on_wait = None
        self.overshoot = 0.0

    def clock(self):
        return self.now

    def factory(self, family, kind):
        if (family, kind) != (te.socket.AF_INET, te.socket.SOCK_DGRAM):
            raise AssertionError("unexpected socket type")
        return self

    def setsockopt(self, *option):
        self.options.append(option)
        if self.option_error:
            raise self.option_error

    def bind(self, address):
        self.binds.append(address)

    def connect(self, peer):
        self.connected = peer

    def setblocking(self, value):
        self.blocking = value

    def send(self, data):
        seq = json.loads(data)["seq"]
        if self.log_path is not None:
            rows = [json.loads(line) for line in self.log_path.read_text().splitlines()]
            if rows[-1]["event"] != "issued" or rows[-1]["seq"] != seq:
                raise AssertionError("send happened before issued was flushed to the raw log")
        self.sent.append((self.now, data, self.connected))
        if seq in self.send_errors:
            raise OSError("injected send failure")
        if seq in self.echo_delay:
            self.queue.append((self.now + self.echo_delay[seq], data, self.connected))
        return len(data)

    def sendto(self, data, peer):
        self.sent.append((self.now, data, peer))
        return len(data)

    def wait(self, readers, writers, errors, timeout):
        self.calls += 1
        if self.calls > 1000:
            raise AssertionError("busy loop")
        if self.on_wait:
            self.on_wait()
        if self.queue and min(row[0] for row in self.queue) <= self.now + timeout:
            self.now = max(self.now, min(row[0] for row in self.queue))
            return readers, [], []
        if timeout <= 0:
            raise AssertionError("non-progressing select without pending I/O")
        self.now += timeout + self.overshoot
        self.overshoot = 0.0
        return [], [], []

    def recvfrom(self, size):
        self.queue.sort(key=lambda row: row[0])
        _at, data, peer = self.queue.pop(0)
        return data[:size], peer

    def recv(self, size):
        return self.recvfrom(size)[0]

    def close(self):
        self.closed = True


class HermeticTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "raw.jsonl"
        # A forgotten injection must fail, never quietly open a real socket.
        self.network_guard = patch.object(te.socket, "socket", side_effect=AssertionError("real socket forbidden"))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    def write_rows(self, rows, suffix=b""):
        self.path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows) + suffix)

    def rows(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def take(self, **kwargs):
        options = dict(log_path=self.path, session_id=COMMON["sessionId"], flow_id=COMMON["flowId"],
                       deadlines_ms=[50], max_age_ms=1000, clock_id=COMMON["clockId"],
                       monotonic=lambda: 1.0)
        options.update(kwargs)
        return te.snapshot(**options)

    def run_client(self, network, **kwargs):
        options = dict(server_ip="192.0.2.10", port=5000, session_id=COMMON["sessionId"],
                       flow_id=COMMON["flowId"], log_path=self.path, clock_id=COMMON["clockId"],
                       duration=0.8, rate_hz=4, reply_drain=0.4,
                       socket_factory=network.factory, wait=network.wait,
                       monotonic=network.clock, interface_reader=lambda _name: dict(IDENTITY))
        options.update(kwargs)
        return te.run_client(**options)

    def run_server(self, network, **kwargs):
        options = dict(bind_ip="192.0.2.10", port=5000, session_id=COMMON["sessionId"],
                       flow_id=COMMON["flowId"], duration=0.2,
                       socket_factory=network.factory, wait=network.wait, monotonic=network.clock)
        options.update(kwargs)
        return te.run_server(**options)


class SnapshotTests(HermeticTest):
    def evidence(self):
        return [start(), heartbeat(0), issue(0, 0), reply(0, 10, 10),
                issue(1, 100), reply(1, 148.5, 48.5), issue(2, 200), reply(2, 251, 51),
                issue(3, 300), issue(4, 400),
                record("send-error", 400, seq=4, error="injected"), heartbeat(450),
                issue(5, 460), reply(3, 480, 180)]

    def test_fold_missing_and_failed_sends_and_alternate_deadlines_from_one_log(self):
        self.write_rows(self.evidence())
        result = self.take(deadlines_ms=[50, 60, 100, 200, 50.0])
        self.assertEqual(result["countersByDeadlineMs"], {
            "50": {"issued": 5, "eligible": 5, "completed": 2},
            "60": {"issued": 5, "eligible": 4, "completed": 3},
            "100": {"issued": 5, "eligible": 4, "completed": 3},
            "200": {"issued": 5, "eligible": 3, "completed": 3},
        })
        self.assertEqual(result["observedAtMs"], 450)
        self.assertEqual(result["remoteNowMs"], 1000)
        self.assertEqual(result["sourceLog"], str(self.path))
        self.assertEqual(result["interface"], IDENTITY)
        self.assertEqual(result["schemaVersion"], "tagged-echo-snapshot/1")

    def test_reader_time_does_not_mature_issues_or_include_suffix(self):
        self.write_rows([start(), issue(0, 400), reply(0, 420, 20), heartbeat(450), issue(1, 500)])
        counters = self.take(deadlines_ms=[60])["countersByDeadlineMs"]["60"]
        self.assertEqual(counters, {"issued": 1, "eligible": 0, "completed": 0})
        self.assertEqual(self.take(monotonic=lambda: 0.6)["countersByDeadlineMs"],
                         self.take(monotonic=lambda: 1.4)["countersByDeadlineMs"])

    def test_exact_duplicates_count_once_even_if_repeated_later(self):
        self.write_rows([start(), issue(0, 0), reply(0, 20, 20), issue(1, 40),
                         issue(0, 0), reply(0, 20, 20),
                         record("send-error", 40, seq=1, error="injected"),
                         record("send-error", 40, seq=1, error="injected"), heartbeat(100)])
        self.assertEqual(self.take()["countersByDeadlineMs"]["50"],
                         {"issued": 2, "eligible": 2, "completed": 1})

    def test_replies_may_arrive_out_of_sequence_but_not_before_their_issue(self):
        self.write_rows([start(), issue(0, 0), issue(1, 10), reply(1, 20, 10),
                         reply(0, 30, 30), heartbeat(100)])
        self.assertEqual(self.take()["countersByDeadlineMs"]["50"]["completed"], 2)
        bad_rows = [
            [start(), reply(0, 10, 10), issue(0, 20), heartbeat(100)],
            [start(), issue(0, 0), reply(1, 20, 20), heartbeat(100)],
            [start(), issue(0, 20), reply(0, 10, 0), heartbeat(100)],
        ]
        for rows in bad_rows:
            with self.subTest(rows=rows):
                self.write_rows(rows)
                with self.assertRaises(te.TaggedEchoError):
                    self.take()

    def test_conflicting_records_and_clock_arithmetic_are_rejected(self):
        bad_rows = [
            [issue(0, 0), issue(0, 1)],
            [issue(0, 0), reply(0, 10, 10), reply(0, 20, 20)],
            [issue(0, 0), reply(0, 20, 10)],
            [issue(0, 0), reply(0, 10, -1)],
            [issue(0, 0), reply(0, 10, float("nan"))],
            [issue(0, 0), record("send-error", 1, seq=0, error="injected"), reply(0, 10, 10)],
            [issue(0, 0), reply(0, 10, 10), record("send-error", 11, seq=0, error="injected")],
            [dict(issue(0, 0), issuedAtMs=1)],
            [issue(True, 0)],
        ]
        for rows in bad_rows:
            with self.subTest(rows=rows):
                self.write_rows([start(), *rows, heartbeat(100)])
                with self.assertRaises(te.TaggedEchoError):
                    self.take()

    def test_every_record_identity_and_schema_are_checked_including_suffix(self):
        for key in ("schemaVersion", "sessionId", "flowId", "clockId"):
            with self.subTest(key=key):
                self.write_rows([start(), heartbeat(100), dict(issue(0, 200), **{key: "foreign"})])
                with self.assertRaises(te.TaggedEchoError):
                    self.take()
        self.write_rows([start(), heartbeat(100)])
        for kwargs in ({"session_id": "foreign"}, {"flow_id": "foreign"}, {"clock_id": "foreign"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(te.TaggedEchoError):
                self.take(**kwargs)

    def test_stale_future_terminal_and_nonrunning_sources_fail(self):
        self.write_rows([start(), heartbeat(100)])
        for kwargs in ({"max_age_ms": 899}, {"monotonic": lambda: 0.09}):
            with self.subTest(kwargs=kwargs), self.assertRaises(te.TaggedEchoError):
                self.take(**kwargs)
        for final in (record("end", 200, status="finished"),
                      heartbeat(200, status="source-failure"), heartbeat(200, status="interrupted")):
            self.write_rows([start(), heartbeat(100), final])
            with self.subTest(final=final), self.assertRaises(te.TaggedEchoError):
                self.take()

    def test_interface_down_ip_change_and_recreated_tun_are_not_attributed(self):
        for changes in ({"up": False}, {"ip": "12.1.1.3"}, {"ifindex": 8}, {"name": "eth0"}):
            self.write_rows([start(), heartbeat(100), heartbeat(200, interface=dict(IDENTITY, **changes))])
            with self.subTest(changes=changes), self.assertRaises(te.TaggedEchoError):
                self.take()

    def test_partial_last_line_only_is_ignored_and_raw_evidence_is_unchanged(self):
        self.write_rows([start(), issue(0, 0), heartbeat(100)], b'{"event":\xff')
        before = self.path.read_bytes()
        self.assertEqual(self.take()["countersByDeadlineMs"]["50"]["issued"], 1)
        self.assertEqual(before, self.path.read_bytes())
        for suffix in (b"bad JSON\n", b"\n", b'{"event":"issued","event":"reply"}\n', b"[]\n"):
            self.write_rows([start(), heartbeat(100)], suffix)
            with self.subTest(suffix=suffix), self.assertRaises(te.TaggedEchoError):
                self.take()

    def test_missing_start_or_heartbeat_unknown_event_and_oversized_log_fail(self):
        for rows in ([], [start()], [heartbeat(100)], [start(), record("other", 10), heartbeat(100)]):
            self.write_rows(rows)
            with self.subTest(rows=rows), self.assertRaises(te.TaggedEchoError):
                self.take()
        self.write_rows([start(), heartbeat(100)])
        before = self.path.read_bytes()
        with patch.object(te, "MAX_LOG_BYTES", len(before) - 1), self.assertRaises(te.TaggedEchoError):
            self.take()
        self.assertEqual(before, self.path.read_bytes())

    def test_deadline_and_freshness_bounds(self):
        self.write_rows([start(), heartbeat(100)])
        for values in ([], [0], [-1], [float("inf")], [float("nan")], [True]):
            with self.subTest(values=values), self.assertRaises(te.TaggedEchoError):
                self.take(deadlines_ms=values)
        for age in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(age=age), self.assertRaises(te.TaggedEchoError):
                self.take(max_age_ms=age)
        self.assertIn("0.123456789012", self.take(deadlines_ms=[0.123456789012])["countersByDeadlineMs"])

    def test_default_clock_must_match_local_boot_explicit_identity_is_supported(self):
        self.write_rows([start(), heartbeat(100)])
        with patch.object(te.Path, "read_text", return_value="test-boot\n") as read_boot:
            self.assertEqual(self.take(clock_id=None)["clockId"], "test-boot")
            read_boot.assert_called_once_with(encoding="ascii")
        with patch.object(te, "boot_clock_id", return_value="different-host-or-boot"):
            with self.assertRaises(te.TaggedEchoError):
                self.take(clock_id=None)
            self.assertEqual(self.take(clock_id="test-boot")["clockId"], "test-boot")

    def test_snapshot_cli_single_compact_json_and_nonzero_failure_without_stdout(self):
        self.write_rows([start(), issue(0, 0), heartbeat(100)])
        argv = ["snapshot", "--log", str(self.path), "--session-id", COMMON["sessionId"],
                "--flow-id", COMMON["flowId"], "--deadlines-ms", "50,60", "--max-age-ms", "1000"]
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(te, "boot_clock_id", return_value="test-boot"), \
                patch.object(te.time, "monotonic", return_value=1.0), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(te.main(argv), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertNotIn(": ", output.getvalue())
        self.assertEqual(set(result["countersByDeadlineMs"]), {"50", "60"})
        self.assertEqual(errors.getvalue(), "")
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(te, "boot_clock_id", return_value="foreign-boot"), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertNotEqual(te.main(argv), 0)
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(errors.getvalue())


class WindowCohortTests(HermeticTest):
    """The judged cohort is what was issued inside the window, or nothing."""

    def evidence(self):
        return [start(), heartbeat(0), issue(0, 0), reply(0, 10, 10),
                issue(1, 100), reply(1, 160, 60), issue(2, 200), reply(2, 210, 10),
                issue(3, 300), heartbeat(400)]

    def test_no_window_keeps_the_board_wide_cohort_and_the_existing_keys(self):
        self.write_rows(self.evidence())
        result = self.take()
        self.assertNotIn("window", result)
        self.assertEqual(result["countersByDeadlineMs"],
                         {"50": {"issued": 4, "eligible": 4, "completed": 2}})

    def test_only_requests_issued_in_the_half_open_window_are_judged(self):
        self.write_rows(self.evidence())
        result = self.take(window_start_ms=100, window_end_ms=300)
        # seq 0 (issued at 0) and seq 3 (issued at 300) are outside [100, 300).
        self.assertEqual(result["countersByDeadlineMs"]["50"],
                         {"issued": 2, "eligible": 2, "completed": 1, "valid": True})
        self.assertEqual(result["window"], dict(
            {"startMs": 100, "endMs": 300, "valid": True}, **self.identity(1, 2, 100, 200)))

    @staticmethod
    def identity(first, last, first_at, last_at):
        import hashlib
        return {"issued": last - first + 1, "firstSeq": first, "lastSeq": last,
                "firstIssuedAtMs": first_at, "lastIssuedAtMs": last_at,
                "seqSha256": hashlib.sha256(",".join(
                    str(seq) for seq in range(first, last + 1)).encode()).hexdigest()}

    def test_synthetic_cohort_identity_is_bounded_and_empty_cohort_has_none(self):
        """Synthetic cohort: count, sequence ends, digest and issue times audit the set."""
        self.write_rows(self.evidence())
        window = self.take(window_start_ms=0, window_end_ms=400)["window"]
        self.assertEqual({key: window[key] for key in self.identity(0, 3, 0, 300)},
                         self.identity(0, 3, 0, 300))
        empty = self.take(window_start_ms=1000, window_end_ms=2000)["window"]
        self.assertEqual((empty["issued"], empty["firstSeq"], empty["lastIssuedAtMs"]),
                         (0, None, None))

    def test_a_request_that_never_answered_in_time_is_a_failure_not_a_gap(self):
        self.write_rows(self.evidence())
        counters = self.take(window_start_ms=100, window_end_ms=400,
                             deadlines_ms=[50])["countersByDeadlineMs"]["50"]
        # seq 3 got its full 50 ms of trailing collection and never answered.
        self.assertEqual(counters, {"issued": 3, "eligible": 3, "completed": 1,
                                    "valid": True})

    def test_collection_that_stopped_before_the_last_deadline_is_invalid(self):
        self.write_rows(self.evidence())
        result = self.take(window_start_ms=100, window_end_ms=400, deadlines_ms=[50, 200])
        self.assertTrue(result["countersByDeadlineMs"]["50"]["valid"])
        # The last issue (300) plus 200 ms reaches past the last heartbeat (400).
        self.assertEqual(result["countersByDeadlineMs"]["200"]["valid"], False)
        self.assertEqual(result["countersByDeadlineMs"]["200"]["invalidReason"],
                         "collection-ended-early")
        self.assertFalse(result["window"]["valid"])
        # A window whose end was never reached cannot be judged either.
        late = self.take(window_start_ms=100, window_end_ms=900)
        self.assertEqual(late["countersByDeadlineMs"]["50"]["invalidReason"],
                         "collection-ended-early")

    def test_zero_issued_requests_establish_neither_zero_nor_success(self):
        self.write_rows(self.evidence())
        result = self.take(window_start_ms=1000, window_end_ms=2000)
        counters = result["countersByDeadlineMs"]["50"]
        self.assertEqual(counters, {"issued": 0, "eligible": 0, "completed": 0,
                                    "valid": False, "invalidReason": "no-issued-requests"})
        self.assertFalse(result["window"]["valid"])

    def test_half_a_window_or_an_empty_interval_is_refused_outright(self):
        self.write_rows(self.evidence())
        for kwargs in ({"window_start_ms": 100}, {"window_end_ms": 300},
                       {"window_start_ms": 300, "window_end_ms": 300},
                       {"window_start_ms": 400, "window_end_ms": 100},
                       {"window_start_ms": -1, "window_end_ms": 100},
                       {"window_start_ms": 0, "window_end_ms": float("inf")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(te.TaggedEchoError):
                self.take(**kwargs)

    def test_the_cli_carries_both_window_bounds_or_the_old_whole_log(self):
        self.write_rows(self.evidence())
        argv = ["snapshot", "--log", str(self.path), "--session-id", COMMON["sessionId"],
                "--flow-id", COMMON["flowId"], "--deadlines-ms", "50",
                "--max-age-ms", "1000"]
        for extra, expected in (([], None),
                                (["--window-start-ms", "100", "--window-end-ms", "300"],
                                 dict({"startMs": 100.0, "endMs": 300.0, "valid": True},
                                      **self.identity(1, 2, 100, 200)))):
            output = io.StringIO()
            with patch.object(te, "boot_clock_id", return_value="test-boot"), \
                    patch.object(te.time, "monotonic", return_value=1.0), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(te.main(argv + extra), 0)
            with self.subTest(extra=extra):
                self.assertEqual(json.loads(output.getvalue()).get("window"), expected)


class ClientTests(HermeticTest):
    def test_bound_client_logs_before_send_folds_replies_and_keeps_misses(self):
        network = FakeNetwork(self.path)
        network.echo_delay = {0: 0.02, 3: 0.08}
        network.send_errors = {2}
        live = []

        def capture():
            if network.now >= 1.0 and not live:
                live.append(self.take(monotonic=network.clock, deadlines_ms=[50, 100]))

        network.on_wait = capture
        self.run_client(network)
        self.assertEqual(network.binds, [(IDENTITY["ip"], 0)])
        self.assertEqual(network.connected, ("192.0.2.10", 5000))
        self.assertIn((te.socket.SOL_SOCKET, te.socket.SO_BINDTODEVICE, b"oaitun_ue1\0"), network.options)
        self.assertFalse(network.blocking)
        self.assertTrue(network.closed)
        rows = self.rows()
        self.assertEqual([row["seq"] for row in rows if row["event"] == "issued"], [0, 1, 2, 3])
        self.assertEqual([row["seq"] for row in rows if row["event"] == "reply"], [0, 3])
        self.assertEqual([row["seq"] for row in rows if row["event"] == "send-error"], [2])
        self.assertEqual(live[0]["countersByDeadlineMs"], {
            "50": {"issued": 4, "eligible": 4, "completed": 1},
            "100": {"issued": 4, "eligible": 4, "completed": 2},
        })
        beats = [row["atMs"] for row in rows if row["event"] == "heartbeat"]
        self.assertEqual(beats, [0, 500, 1000])
        self.assertEqual(rows[-1]["event"], "end")
        self.assertNotEqual(rows[-1]["status"], "running")
        self.assertAlmostEqual(network.now, 1.2)
        for row in rows:
            for key, value in COMMON.items():
                self.assertEqual(row[key], value)
        with self.assertRaises(te.TaggedEchoError):
            self.take(monotonic=network.clock)

    def test_untrusted_duplicate_and_failed_send_replies_are_not_new_requests(self):
        network = FakeNetwork(self.path)
        good = te.make_packet(COMMON["sessionId"], COMMON["flowId"], 0, 256)
        foreign = te.make_packet("foreign", COMMON["flowId"], 0, 256)
        unknown = te.make_packet(COMMON["sessionId"], COMMON["flowId"], 50, 256)
        network.queue = [(0.01, b"bad", None), (0.02, foreign, None), (0.03, unknown, None),
                         (0.04, good + b" ", None), (0.05, good, None), (0.06, good, None)]
        self.run_client(network, duration=0.1, rate_hz=1, reply_drain=0.1)
        self.assertEqual(len([row for row in self.rows() if row["event"] == "issued"]), 1)
        replies = [row for row in self.rows() if row["event"] == "reply"]
        self.assertEqual(len(replies), 1)
        self.assertAlmostEqual(replies[0]["rttMs"], 50)
        other = Path(self.temp.name) / "failed.jsonl"
        network = FakeNetwork(other)
        network.send_errors = {0}
        network.queue = [(0.05, good, None)]
        self.run_client(network, log_path=other, duration=0.1, reply_drain=0.1)
        self.assertNotIn('"event":"reply"', other.read_text())

    def test_exclusive_output_refuses_existing_file_and_symlink_without_sending(self):
        self.path.write_text("preserved raw evidence")
        for path in (self.path, Path(self.temp.name) / "link.jsonl"):
            if path != self.path:
                path.symlink_to(self.path)
            network = FakeNetwork(path)
            with self.subTest(path=path), self.assertRaises(FileExistsError):
                self.run_client(network, log_path=path)
            self.assertFalse(network.sent)
            self.assertFalse(network.binds)
            self.assertEqual(self.path.read_text(), "preserved raw evidence")

    def test_interface_loss_waits_and_identity_change_rebinds(self):
        # docs/design/ue-identity-continuity.md: a re-registered UE is followed, never a
        # source failure; pending requests are voided and nothing is issued while unbound.
        for changes in ({"up": False}, {"ip": "12.1.1.3"}, {"ifindex": 8}, None):
            with self.subTest(changes=changes):
                path = Path(self.temp.name) / f"source-{len(os.listdir(self.temp.name))}.jsonl"
                network = FakeNetwork(path)

                def interface(_name):
                    if network.now < 0.5:
                        return dict(IDENTITY)
                    if changes is None:
                        raise OSError("tun missing")
                    return dict(IDENTITY, **changes)

                self.run_client(network, log_path=path, interface_reader=interface)
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                events = [row["event"] for row in rows]
                self.assertEqual(rows[-1]["status"], "finished")
                self.assertEqual(events.count("rebind-wait"), 1)
                self.assertNotIn("source-failure", [row.get("status") for row in rows])
                if changes is None or changes == {"up": False}:
                    self.assertNotIn("rebind", events)
                    self.assertTrue(all(at < 0.5 for at, _data, _peer in network.sent))
                    self.assertFalse([row for row in rows[events.index("rebind-wait"):]
                                      if row["event"] == "heartbeat"])
                else:
                    rebind = rows[events.index("rebind")]
                    self.assertEqual((rebind["bindEpoch"], rebind["interface"]),
                                     (1, dict(IDENTITY, **changes)))
                    self.assertEqual(len(network.binds), 2)
                    self.assertTrue(any(at >= 0.5 for at, _data, _peer in network.sent))
                self.assertTrue(network.closed)

    def test_bind_device_failure_is_terminal_no_fallback_socket(self):
        network = FakeNetwork(self.path)
        network.option_error = PermissionError("bind device not permitted")
        with self.assertRaises(te.TaggedEchoError):
            self.run_client(network)
        self.assertFalse(network.sent)
        self.assertFalse(network.binds)
        self.assertEqual(self.rows()[-1]["status"], "source-failure")
        self.assertTrue(network.closed)

    def test_rate_payload_duration_and_drain_bounds_refuse_before_io(self):
        cases = [{"rate_hz": 21}, {"rate_hz": 0}, {"rate_hz": float("nan")},
                 {"duration": 3601}, {"duration": 0}, {"duration": float("inf")},
                 {"duration": 3600, "reply_drain": 1}, {"reply_drain": 11}, {"reply_drain": -1},
                 {"payload_bytes": 1201}, {"payload_bytes": 1}, {"payload_bytes": True},
                 {"server_ip": "0.0.0.0"}, {"server_ip": "example.invalid"},
                 {"interface": "eth0"}, {"port": 0}]
        for kwargs in cases:
            network = FakeNetwork(self.path)
            with self.subTest(kwargs=kwargs), self.assertRaises(te.TaggedEchoError):
                self.run_client(network, **kwargs)
            self.assertFalse(self.path.exists())
            self.assertFalse(network.binds)
        te._limits(3590, 20, 1200, 10)  # Inclusive upper limits without a long run.

    def test_max_rate_is_paced_and_deadline_overshoot_does_not_busy_loop(self):
        network = FakeNetwork(self.path)
        self.run_client(network, rate_hz=20, payload_bytes=1200, duration=0.3, reply_drain=0)
        self.assertLessEqual(len(network.sent), 6)
        self.assertTrue(all(len(data) == 1200 for _at, data, _peer in network.sent))
        for previous, current in zip(network.sent, network.sent[1:]):
            self.assertGreaterEqual(current[0] - previous[0] + 1e-12, 0.05)
        # A delayed scheduler jumps past the final issue slot into receive-only
        # drain. It must not spin forever waiting for the stale send deadline.
        other = Path(self.temp.name) / "delayed.jsonl"
        network = FakeNetwork(other)
        network.overshoot = 0.7
        self.run_client(network, log_path=other, duration=0.3, reply_drain=0.7)
        self.assertEqual(len(network.sent), 1)
        self.assertLess(network.calls, 10)
        self.assertAlmostEqual(network.now, 1.0)

    def test_linux_interface_reader_uses_flags_address_and_ifindex_with_mocked_ioctl(self):
        def ioctl(_control, operation, request):
            self.assertTrue(request.startswith(b"oaitun_ue1\0"))
            result = bytearray(256)
            if operation == 0x8913:
                struct.pack_into("H", result, 16, 1)
            elif operation == 0x8933:
                struct.pack_into("i", result, 16, 7)
            elif operation == 0x8915:
                result[20:24] = bytes([12, 1, 1, 2])
            else:
                self.fail("unexpected ioctl")
            return bytes(result)

        with patch.object(te.socket, "socket", return_value=MagicMock()), \
                patch.object(te.fcntl, "ioctl", side_effect=ioctl):
            self.assertEqual(te.read_interface(), IDENTITY)


class ServerTests(HermeticTest):
    def test_fixed_bind_allowlist_tags_sizes_runtime_and_aggregate_rate_cap(self):
        network = FakeNetwork()
        packet = lambda seq, size=256: te.make_packet(COMMON["sessionId"], COMMON["flowId"], seq, size)
        peer = ("12.1.1.2", 40000)
        network.queue = [
            (0, packet(0), peer), (0.01, packet(1), ("12.1.1.3", 40001)),
            (0.06, packet(2), ("198.51.100.5", 40000)),
            (0.07, te.make_packet("foreign", COMMON["flowId"], 3, 256), peer),
            (0.08, packet(4, 1200) + b" ", peer), (0.09, b"bad JSON", peer),
            (0.10, packet(5), peer), (0.16, packet(6, 1200), peer), (0.25, packet(7), peer),
        ]
        self.run_server(network)
        self.assertEqual(network.binds, [("192.0.2.10", 5000)])
        self.assertEqual([json.loads(data)["seq"] for _at, data, _peer in network.sent], [0, 5, 6])
        self.assertEqual([data for _at, data, _peer in network.sent], [packet(0), packet(5), packet(6, 1200)])
        self.assertTrue(all(len(data) <= 1200 for _at, data, _peer in network.sent))
        self.assertAlmostEqual(network.now, 0.2)
        self.assertTrue(network.closed)
        self.assertFalse(network.blocking)

    def test_server_rejects_unbounded_wildcard_dns_and_bad_subnet_configuration(self):
        for kwargs in ({"bind_ip": "0.0.0.0"}, {"bind_ip": "224.1.1.1"},
                       {"bind_ip": "example.invalid"}, {"duration": 3601},
                       {"rate_hz": 21}, {"rate_hz": float("nan")},
                       {"allowed_subnet": "bad"}, {"port": 65536}):
            network = FakeNetwork()
            with self.subTest(kwargs=kwargs), self.assertRaises(te.TaggedEchoError):
                self.run_server(network, **kwargs)
            self.assertFalse(network.binds)


if __name__ == "__main__":
    unittest.main()
