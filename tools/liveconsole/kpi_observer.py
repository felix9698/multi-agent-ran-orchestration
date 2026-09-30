"""What a trial's KPI vector is read from, in ``MOCK`` and on the radio.

One interface -- :class:`KpiObserver`, a ``sample()`` returning
``{"<kpi>@<ue>": value}`` -- so the executor
(:mod:`tools.liveconsole.agent`) does the same thing in both session modes:
sample once per observation poll, aggregate the samples of the hold into one
vector, judge that vector against every authorized target.

In ``MOCK`` the observer is the emulator's
(:class:`tools.hfconsole.agent_env.EmulatedKpiObserver`).  On the radio it is
:class:`TunRateObserver`, which reads exactly what
``scripts/hardware/sample_ue_throughput.sh`` reads and for exactly the reason
that script gives: KPM's ``DRB.UEThpDl`` has been observed reading 0 on this
deployment while the UE's own ``oaitun_ue1`` counter advanced at ~6 Mbps, so
the honest per-UE downlink is the tun ``rx_bytes`` counter differenced over
time (``CLAUDE.md``, "처리량은 UE tun의 rx_bytes 차분으로").

Tun counters remain the operational cross-check, not exact service payload.
Explicit ``liveConsole.flowGoodput`` sources select per-flow application bytes
from a separately launched ``flow_goodput.py`` receiver. Its same-UE-clock
snapshots retain tun totals alongside payload counters; other flows/overhead
never count towards that source's application goodput.

The serving cell comes from the KPM attribution reader the sitting already
owns; nothing here parses an indication itself.

Scenario I4 uses an explicitly launched ``tagged_echo.py`` source on the UE.
:class:`LiveTaggedEchoObserver` reads its session-scoped raw-log snapshot and
keeps counters at every authorized deadline. The default, unconfigured path
refuses I4; a configured but stale/unreadable source contributes no KPI.
:class:`TaggedEchoObserver` retains the legacy injected-log interface for
hardware-free fixtures, not for the real profile composition.

Nothing here is a verdict.  A UE whose counter could not be read contributes
**no** key, and a missing key is ``UNKNOWN`` when the target is judged -- a
sample that did not happen never establishes a success.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Protocol, Sequence, Tuple)

from tools.liveconsole import flow_goodput

__all__ = [
    "DEADLINE_RATIO_KPI",
    "GOODPUT_KPI",
    "LIVE_UNOBSERVED_KPIS",
    "SERVING_CELL_KPI",
    "CELL_GOODPUT_KPI",
    "CELL_TX_ATTENUATION_KPI",
    "KpmCellAttenuationObserver",
    "cell_attenuations_from_lines",
    "cell_goodput_totals",
    "KpiObserver",
    "KpiObserverError",
    "TaggedEchoObserver",
    "LiveFlowGoodputObserver",
    "TunRateObserver",
    "live_flow_sources",
    "build_live_observer",
    "refuse_unobserved_kpis",
    "resolve_ue_hosts",
    "serving_cells_from_reader",
    "ue_hosts_from_env",
    "ue_hosts_from_profile",
]

#: The two KPIs this build can both emulate and observe live (contract 2.1).
GOODPUT_KPI = "dlGoodputMbps"

#: tun 이 이만큼 넘게 받았는데 우리 flow 의 payload 가 한 바이트도 안 늘었다면, 0 은
#: 네트워크의 값이 아니라 우리 송신기가 낡은 주소로 쏘고 있다는 신호다.  10 Mbps 부하
#: 아래 한 창(수 초)이면 수 MB 가 흐르므로 64 KiB 는 넉넉히 보수적인 경계다.
_TUN_ADVANCE_WITHOUT_FLOW_BYTES = 64 * 1024
#: The longest same-connection payload stall read as TCP head-of-line waiting (v5.2: 4-8 s).
_HOL_STALL_MAX_MS = 15000.0
SERVING_CELL_KPI = "servingCell"

#: Scenario I4's deadline-success ratio.  It is emulated and it is predicted;
#: it is **not** observed on this deployment (see :data:`LIVE_UNOBSERVED_KPIS`).
DEADLINE_RATIO_KPI = "deadlineSuccessRatio"

#: v4 delivery continuity.  The observer publishes the SAME per-sample goodput
#: under this second key so the measurement rule's ``lowRun`` statistic can ask
#: a different question of it -- how long the shortfall ran, not how deep it was
#: on average.  No extra read, no extra ssh: one number, two questions.
LOW_DELIVERY_RUN_KPI = "lowDeliveryRunBins"

#: 셀이 실어 나른 하향 총량.  **새 계측이 아니다** -- 이미 UE 안에서 재고 있는
#: :data:`GOODPUT_KPI` 표본을 :data:`SERVING_CELL_KPI` 가 말해 주는 소속으로 묶어
#: 더한 값이다.  owner 가 UE 가 아니라 셀 사업자인 유일한 KPI 라 키는
#: ``cellGoodputMbps@<nci>`` 다.
CELL_GOODPUT_KPI = "cellGoodputMbps"

#: 셀의 설정된 송신 감쇠(dB) -- KPM ``RAN.Cell.TxAttenuationDb`` 되읽기.  v4.7 사업자
#: 에너지 인텐트의 KPI 이고 키는 ``cellTxAttenuationDb@<nci>`` 다
#: (``assurance.coordination.tc.KPI_CELL_TX_ATTENUATION``).
CELL_TX_ATTENUATION_KPI = "cellTxAttenuationDb"
KPM_TX_ATTENUATION_COUNTER = "RAN.Cell.TxAttenuationDb"

#: Every KPI kind the live path must refuse **by name**, with why.  The same
#: rule ``tools/liveconsole/agent.py``'s ``LIVE_UNCARRIED_AXES`` states for an
#: action axis nothing on the wire can read back, and for the same reason: a
#: KPI nobody can measure is not a KPI this sitting may judge a target on, and
#: substituting the emulator's number for it would turn emulator evidence into
#: an OTA claim.  A refusal is the honest outcome; a mock is not.
LIVE_UNOBSERVED_KPIS: Mapping[str, str] = {
    DEADLINE_RATIO_KPI: (
        "no tagged-echo traffic generator is configured in liveConsole.taggedEcho; "
        "an explicit session/flow source must write the raw log a deadline is "
        "measured from (a bulk iperf3 flow carries no per-request tag and the "
        "single-ping collection this build has is neither a deadline-success "
        "nor a p95 measurement)"),
}

#: The tun device every OAI UE in this lab exposes.
DEFAULT_IFACE = "oaitun_ue1"

#: What one line of the echo log the generator would write has to carry, in the
#: order each is looked for.  A single-clock round trip: the UE stamps the
#: request out and the reply in against *its own* clock and the log records the
#: difference, because nothing in this lab time-synchronizes the two ends well
#: enough to infer a one-way delay from a two-clock difference
#: (``exp_3UEscenario.md``: "RTT uses a single-clock echo measurement; do not
#: infer one-way delay without time synchronization").
_ECHO_SEQUENCE_KEYS: Tuple[str, ...] = ("seq", "sequence", "id")
_ECHO_RTT_KEYS: Tuple[str, ...] = ("rttMs", "rtt_ms", "rtt")
_ECHO_ISSUED_KEYS: Tuple[str, ...] = ("issuedAtMs", "issued_at_ms", "t", "at")


def _ssh_failure(ue: str, alias: str, completed: Any) -> Dict[str, Any]:
    """One observer failure that says *why*, not just that ssh returned non-zero.

    ``error`` used to read "ssh exit 1" and nothing else, which sends a reader
    after the transport.  The reason was already in ``stderr`` one field away:
    2026-09-18 board 20260918T100431 carries 70 of these on ue1, every one of
    them "flow-goodput: no complete running heartbeat" -- the UE's traffic
    source was not running, because ue1 had dropped.  The exit code stays in
    front so anything already matching on "ssh exit" keeps working.
    """
    detail = str(getattr(completed, "stderr", "") or "")
    lines = [line for line in detail.strip().splitlines() if line.strip()]
    code = f"ssh exit {getattr(completed, 'returncode', None)}"
    return {"ueId": ue, "host": alias,
            "error": f"{code}: {lines[0].strip()}" if lines else code,
            "stderr": detail[:200]}


class KpiObserverError(RuntimeError):
    """A KPI this sitting was asked to observe cannot be observed here."""


class KpiObserver(Protocol):
    """One sample of the KPIs a target can be judged on."""

    def sample(self) -> Dict[str, Any]:
        ...


# --------------------------------------------------------------------------- #
# where the UE hosts come from
# --------------------------------------------------------------------------- #


def ue_hosts_from_profile(document: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """``liveConsole.ueHosts`` of the live profile: ``{"<amfUeNgapId>": "ue1"}``."""
    console = dict((document or {}).get("liveConsole") or {})
    hosts = console.get("ueHosts") or (document or {}).get("ueHosts") or {}
    if not isinstance(hosts, Mapping):
        return {}
    return {str(ue): str(alias) for ue, alias in hosts.items() if str(alias).strip()}


def ue_hosts_from_env(ue_ids: Sequence[str],
                      env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """``HW_UE<n>_HOST`` of ``scripts/hardware/env.sh``, by position.

    The lab's env names its UE hosts ``HW_UE1_HOST`` / ``HW_UE2_HOST`` -- ``1``
    and ``2`` are the UE *machines*, not amfUeNgapIds, so the mapping is
    positional over the UEs this sitting addresses, in id order.
    """
    env = os.environ if env is None else env
    resolved: Dict[str, str] = {}
    for position, ue_id in enumerate(sorted(ue_ids, key=lambda item: (len(str(item)), str(item))), 1):
        alias = str(env.get(f"HW_UE{position}_HOST", "") or "").strip()
        if alias:
            resolved[str(ue_id)] = alias
    return resolved


def resolve_ue_hosts(ue_ids: Sequence[str], *,
                     profile_document: Optional[Mapping[str, Any]] = None,
                     env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The profile's own mapping first, the lab env second, nothing invented."""
    wanted = [str(item) for item in ue_ids]
    named = ue_hosts_from_profile(profile_document)
    resolved = {ue: named[ue] for ue in wanted if ue in named}
    if len(resolved) == len(wanted):
        return resolved
    # 자리는 **이 판이 다루는 UE 전체**에 대해 매기고, 그 뒤에 아직 안 정해진 UE 만
    # 채운다.  미해결 UE 만 넘기면 거기서 1 부터 다시 번호가 매겨져, 프로파일이 ue1 만
    # 이름 붙인 경우 ue2 에 `HW_UE1_HOST` -- ue1 의 기계 -- 가 붙는다.  그러면 ue1 의
    # 처리량이 ue2 의 goodput 으로 기록되고 아무 데서도 안 터진다 (2026-09-22 codex 감사).
    # 랩 프로파일은 세 UE 를 다 이름 붙이므로 이 분기는 평소 쓰이지 않지만, 쓰이는
    # 날에 조용히 틀리는 것이 문제였다.
    from_env = ue_hosts_from_env(wanted, env)
    resolved.update({ue: host for ue, host in from_env.items() if ue not in resolved})
    return resolved


def serving_cells_from_reader(reader: Any) -> Callable[[], Dict[str, str]]:
    """``{"<amfUeNgapId>": "<nci>"}`` from the sitting's attribution reader."""

    def read() -> Dict[str, str]:
        try:
            observations = reader.refresh()
        except Exception:  # a stalled tail is a missing sample, not a failure
            return {}
        return {str(item.amf_ue_ngap_id): str(item.serving_nci) for item in observations}

    return read


# --------------------------------------------------------------------------- #
# the live observer
# --------------------------------------------------------------------------- #


@dataclass
class TunRateObserver:
    """Per-UE downlink from the UE tun counter, differenced between samples.

    ``runner`` is anything with ``run(argv, capture_output=True, text=True,
    timeout=...)`` returning an object carrying ``returncode`` and ``stdout``
    -- :mod:`subprocess` by default, a fake in the unit test, so nothing here
    ever opens ssh under ``python3 -m unittest``.

    The first sample of a UE has nothing to difference against and therefore
    reports no goodput for it; from the second on, the rate is the byte delta
    over the elapsed wall time between the two reads.
    """

    hosts: Mapping[str, str] = field(default_factory=dict)
    iface: str = DEFAULT_IFACE
    runner: Any = subprocess
    serving_cells: Optional[Callable[[], Mapping[str, str]]] = None
    monotonic_ms: Callable[[], float] = lambda: time.monotonic() * 1000.0
    connect_timeout_s: int = 5
    timeout_s: float = 15.0
    #: LIVE additionally checks UP/IP/ifindex and uses the UE's own clock.
    verify_interface: bool = False
    #: ``{ue: (rx_bytes, at_ms, identity)}`` of the previous successful read.
    previous: Dict[str, Any] = field(default_factory=dict)
    #: One line per unreadable UE, kept for the evidence and never raised.
    failures: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.hosts = {str(ue): str(alias) for ue, alias in dict(self.hosts).items()}

    # -- one read ---------------------------------------------------------- #

    def command(self, alias: str) -> List[str]:
        command = f"cat /sys/class/net/{self.iface}/statistics/rx_bytes"
        if self.verify_interface:
            script = (
                "import json,pathlib,subprocess,sys,time; iface=sys.argv[1]; "
                "rows=json.loads(subprocess.check_output(['ip','-j','-4','addr',"
                "'show','up','dev',iface],text=True)); "
                "row=rows[0]; addresses=sorted(a['local'] for a in row['addr_info'] "
                "if a['family']=='inet'); "
                "assert addresses, 'no UP tun IPv4 address'; "
                "base=pathlib.Path('/sys/class/net')/iface; "
                "print(json.dumps({'rx':int((base/'statistics/rx_bytes').read_text()), "
                "'atMs':time.monotonic()*1000,'ifindex':row['ifindex'],"
                "'addresses':addresses,'clockId':pathlib.Path("
                "'/proc/sys/kernel/random/boot_id').read_text().strip()}))")
            command = shlex.join(["python3", "-c", script, self.iface])
        return ["ssh", "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={int(self.connect_timeout_s)}", alias, command]

    def _rx_bytes(self, ue: str, alias: str) -> Any:
        try:
            completed = self.runner.run(self.command(alias), capture_output=True,
                                        text=True, timeout=self.timeout_s)
        except Exception as exc:  # an unreachable UE is a missing sample
            self.failures.append({"ueId": ue, "host": alias, "error": f"{type(exc).__name__}: {exc}"})
            return None
        if int(getattr(completed, "returncode", 1) or 0) != 0:
            self.failures.append(_ssh_failure(ue, alias, completed))
            return None
        text = str(getattr(completed, "stdout", "") or "").strip().splitlines()
        try:
            if self.verify_interface:
                row = json.loads(text[0])
                if (int(row['rx']) < 0 or int(row['ifindex']) <= 0
                        or not row['addresses'] or not row['clockId']
                        or not math.isfinite(float(row['atMs']))):
                    raise ValueError("invalid tun identity/counter")
                return row
            return int(text[0].strip())
        except (IndexError, ValueError, KeyError, TypeError):
            self.failures.append({"ueId": ue, "host": alias,
                                  "error": f"unreadable counter {text[:1]!r}"})
            return None

    def sample(self) -> Dict[str, Any]:
        """One KPI vector: a rate per readable UE, a serving cell per attributed UE."""
        now = float(self.monotonic_ms())
        observed: Dict[str, Any] = {}
        # Independent SSH reads share one sampling interval. Serial round trips
        # add every UE's latency to the Kernel's observation cadence.
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(self.hosts)))) as pool:
            reads = {ue: pool.submit(self._rx_bytes, ue, alias)
                     for ue, alias in self.hosts.items()}
            counters = {ue: pending.result() for ue, pending in reads.items()}
        for ue, counter in counters.items():
            if counter is None:
                self.previous.pop(ue, None)
                continue
            at, identity = now, None
            if self.verify_interface:
                identity = (counter['clockId'], counter['ifindex'], tuple(counter['addresses']))
                at, counter = float(counter['atMs']), int(counter['rx'])
            before = self.previous.get(ue)
            self.previous[ue] = (counter, at, identity)
            if not before or before[2] != identity:
                continue
            delta_bytes, elapsed_ms = counter - before[0], at - before[1]
            if elapsed_ms <= 0 or delta_bytes < 0:
                continue
            observed[f"{GOODPUT_KPI}@{ue}"] = delta_bytes * 8.0 / 1e6 / (elapsed_ms / 1000.0)
        if self.serving_cells is not None:
            for ue, cell in dict(self.serving_cells() or {}).items():
                observed[f"{SERVING_CELL_KPI}@{ue}"] = str(cell)
        return observed

    def describe(self) -> Dict[str, Any]:
        return {"kind": GOODPUT_KPI, "measurementDefinition": "tun-aggregate-rx-bytes",
                "hosts": dict(self.hosts), "interface": self.iface,
                "verifyInterface": self.verify_interface}


# --------------------------------------------------------------------------- #
# scenario I4 -- the tagged echo flow
# --------------------------------------------------------------------------- #


@dataclass
class TaggedEchoObserver:
    """``deadlineSuccessRatio@<ue>`` from the echo log the generator writes.

    Composed exactly the way :class:`TunRateObserver` is: a ``{ue: host}``
    mapping, an injectable ``runner`` so nothing here opens ssh under
    ``python3 -m unittest``, and a ``sample()`` that answers only for the UEs
    it could actually read.

    What it reads is one JSON object per line -- ``{"seq": 41, "issuedAtMs":
    1725..., "rttMs": 12.4}`` -- appended by the traffic generator on the UE
    itself.  A request that never answered is written with **no** ``rttMs`` (or
    a null one) and that is the whole reason the log is read rather than a
    summary: the denominator is every eligible issued request, the ones with no
    response included.  ``rttMs`` is a single-clock round trip measured at the
    UE; no one-way delay is inferred from it.

    The sample is the two cumulative counters, not a ratio, because the window
    statistic is ``completed / eligible`` over the window and a mean of
    per-sample ratios is a different number.  ``deadline_ms`` is what the
    target in force names -- the executor sets it before the hold and the
    counters are recomputed from the log's own round trips, which is what lets
    one log answer for every authorized deadline level.

    **Nothing composes this on the radio today.**  No generator writes that
    log, so :func:`build_live_observer` refuses the KPI by name; this class is
    the shape the generator has to fill and is exercised only against an
    injected log source.
    """

    hosts: Mapping[str, str] = field(default_factory=dict)
    deadline_ms: float = 0.0
    log_path: str = "/tmp/tagged-echo.jsonl"
    runner: Any = subprocess
    monotonic_ms: Callable[[], float] = lambda: time.monotonic() * 1000.0
    connect_timeout_s: int = 5
    timeout_s: float = 15.0
    #: One line per unreadable UE, kept for the evidence and never raised.
    failures: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.hosts = {str(ue): str(alias) for ue, alias in dict(self.hosts).items()}
        self.deadline_ms = float(self.deadline_ms or 0.0)

    def command(self, alias: str) -> List[str]:
        return ["ssh", "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={int(self.connect_timeout_s)}", alias,
                f"cat {self.log_path}"]

    def _lines(self, ue: str, alias: str) -> Optional[Iterable[str]]:
        try:
            completed = self.runner.run(self.command(alias), capture_output=True,
                                        text=True, timeout=self.timeout_s)
        except Exception as exc:  # an unreachable UE is a missing sample
            self.failures.append({"ueId": ue, "host": alias,
                                  "error": f"{type(exc).__name__}: {exc}"})
            return None
        if int(getattr(completed, "returncode", 1) or 0) != 0:
            self.failures.append(_ssh_failure(ue, alias, completed))
            return None
        return str(getattr(completed, "stdout", "") or "").splitlines()

    def _lines_by_ue(self) -> Dict[str, Optional[Iterable[str]]]:
        """Every host's snapshot read at once; processing stays in host order.

        A serial round trip per UE (~0.5 s each over SSH) made one poll take
        1.5-2 s against a 1 s cadence, so a live hold held a third fewer
        samples than it owed: 2026-09-15 attempt 32 judged goodput at coverage
        0.667 and left I1g/I2g/I3g UNKNOWN.  ``TunRateObserver`` already reads
        in parallel for the same reason.
        """
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(self.hosts)))) as pool:
            pending = {ue: pool.submit(self._lines, ue, alias) for ue, alias in self.hosts.items()}
            return {ue: future.result() for ue, future in pending.items()}

    def counters(self, lines: Iterable[str], *, now_ms: Optional[float] = None
                 ) -> Dict[str, int]:
        """``{"issued", "eligible", "completed"}`` over one log.

        A request is **eligible** once its deadline has passed, answered or
        not; until then it is neither a success nor a miss and counting it
        either way would make a ratio out of requests still in flight.  A line
        this cannot read is skipped and does not silently become a miss.

        One request may span several lines: the generator writes the issue and
        its reply as separate records carrying the same sequence number, so the
        rows are **merged** per sequence rather than deduplicated down to the
        first one -- dropping the later row would drop every ``rttMs`` and make
        ``completed`` structurally zero.  A record that carries no sequence, no
        issue time and no round trip (a start, heartbeat or end marker) is not
        a request and never enters the denominator.
        """
        deadline = float(self.deadline_ms)
        now = float(self.monotonic_ms() if now_ms is None else now_ms)
        requests: Dict[Any, Dict[str, Optional[float]]] = {}
        for line in lines or ():
            row = _echo_row(line)
            if row is None:
                continue
            sequence = next((row[key] for key in _ECHO_SEQUENCE_KEYS if key in row),
                            None)
            issued_at = next((_number(row[key]) for key in _ECHO_ISSUED_KEYS
                              if key in row), None)
            rtt = next((_number(row[key]) for key in _ECHO_RTT_KEYS
                        if row.get(key) is not None), None)
            if sequence is None and issued_at is None and rtt is None:
                continue                     # a source marker, not a request
            key = ("seq", sequence) if sequence is not None else ("row", len(requests))
            entry = requests.setdefault(key, {"issuedAt": None, "rtt": None})
            for name, value in (("issuedAt", issued_at), ("rtt", rtt)):
                if entry[name] is None:
                    entry[name] = value
        issued = eligible = completed = 0
        for entry in requests.values():
            issued += 1
            if (deadline > 0 and entry["issuedAt"] is not None
                    and entry["issuedAt"] + deadline > now):
                continue                     # still in flight; not yet eligible
            eligible += 1
            if entry["rtt"] is not None and deadline > 0 and entry["rtt"] <= deadline:
                completed += 1
        return {"issued": issued, "eligible": eligible, "completed": completed}

    def sample(self) -> Dict[str, Any]:
        """One KPI vector: the two counters per readable UE, nothing invented."""
        if self.deadline_ms <= 0:
            raise KpiObserverError(
                f"{DEADLINE_RATIO_KPI} has no deadline to be measured within; the "
                "target in force names one and the executor sets it before the hold")
        now = float(self.monotonic_ms())
        observed: Dict[str, Any] = {}
        for ue, alias in self.hosts.items():
            lines = self._lines(ue, alias)
            if lines is None:
                continue
            observed[f"{DEADLINE_RATIO_KPI}@{ue}"] = self.counters(lines, now_ms=now)
        return observed


@dataclass
class LiveTaggedEchoObserver(TaggedEchoObserver):
    """Read an explicitly launched source on the UE, not an injected log.

    The snapshot command folds the append-only raw log on its originating
    host. Both eligibility and RTT use that host's monotonic clock. No process
    is launched or repaired by this observer.
    """

    source_path: str = ""
    session_id: str = ""
    flow_id: str = ""
    deadlines_ms: Tuple[float, ...] = ()
    max_age_ms: float = 1500.0
    previous_source: Dict[str, Any] = field(default_factory=dict)
    #: The judged window, on the **source UE's own monotonic clock** -- the one
    #: ``issuedAtMs`` is written against, never the coordinator's.  Both bounds
    #: or neither: the source refuses half a window rather than guessing at the
    #: other edge, so nothing here invents one.  Unset (the default) asks for
    #: the whole-log cumulative counters exactly as before.
    window_start_ms: Optional[float] = None
    window_end_ms: Optional[float] = None

    def command(self, alias: str) -> List[str]:
        argv = ["python3", self.source_path, "snapshot", "--log", self.log_path,
                "--session-id", self.session_id, "--flow-id", self.flow_id,
                "--deadlines-ms", ",".join(format(d, '.12g') for d in self.deadlines_ms),
                "--max-age-ms", str(self.max_age_ms)]
        if self.window_start_ms is not None and self.window_end_ms is not None:
            argv += ["--window-start-ms", format(float(self.window_start_ms), '.12g'),
                     "--window-end-ms", format(float(self.window_end_ms), '.12g')]
        return ["ssh", "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={int(self.connect_timeout_s)}", alias,
                shlex.join(argv)]

    def sample(self) -> Dict[str, Any]:
        observed = {}
        read = self._lines_by_ue()
        for ue, alias in self.hosts.items():
            lines = read.get(ue)
            if lines is None:
                continue
            try:
                row = json.loads("\n".join(lines))
                if (row.get("schemaVersion") != "tagged-echo-snapshot/1"
                        or row.get("status") != "running"
                        or row.get("sessionId") != self.session_id
                        or row.get("flowId") != self.flow_id
                        or not row.get("clockId")):
                    raise ValueError("wrong tagged-echo session/flow/source state")
                at, now = float(row["observedAtMs"]), float(row["remoteNowMs"])
                if not (math.isfinite(at) and math.isfinite(now)
                        and 0 <= now - at <= self.max_age_ms):
                    raise ValueError("stale tagged-echo source")
                source = {key: row[key] for key in ("sessionId", "flowId", "clockId")}
                source["bindEpoch"] = int(row.get("bindEpoch", 0))
                before = self.previous_source.get(ue)
                # A re-registered UE's client rebinds to its new tun under the same log
                # and boot: a higher bind epoch is that, not a foreign source.  The
                # change of source still makes a window that spans it unjudgeable.
                rebound = bool(before) and source["bindEpoch"] > before[0].get("bindEpoch", 0) \
                    and all(source[key] == before[0][key] for key in ("sessionId", "flowId", "clockId"))
                if before and ((source != before[0] and not rebound) or at <= before[1]):
                    raise ValueError("changed source clock or non-advancing heartbeat")
                counters = {}
                for deadline in self.deadlines_ms:
                    wire = format(deadline, '.12g')
                    # The wire key is 12 significant digits; downstream matches the configured
                    # deadline within 1e-9 ms, which a 4-digit d (1886.495219051838 -> wire
                    # 1886.49521905, 1.8e-9 off) misses -- every I2d of block 15's first board
                    # (2026-09-26) came out UNKNOWN on valid counts.  Report under the
                    # configured value itself.
                    key = wire if float(wire) == float(deadline) else repr(float(deadline))
                    counts = dict(row['countersByDeadlineMs'][wire])
                    values = [counts[name] for name in ('issued', 'eligible', 'completed')]
                    if (any(isinstance(n, bool) or not isinstance(n, int) for n in values)
                            or not 0 <= values[2] <= values[1] <= values[0]):
                        raise ValueError("invalid tagged-echo counters")
                    counters[key] = counts
                self.previous_source[ue] = (source, at)
                observed[f"{DEADLINE_RATIO_KPI}@{ue}"] = {
                    "byDeadlineMs": counters, "source": source, "observedAtMs": at,
                    "sourceLog": row.get("sourceLog", self.log_path),
                    "interface": row.get("interface")}
                if "window" in row:
                    # The windowed read's cohort identity, kept for the audit.
                    observed[f"{DEADLINE_RATIO_KPI}@{ue}"]["window"] = dict(row["window"])
            except (ValueError, TypeError, KeyError) as exc:
                self.failures.append({"ueId": ue, "host": alias, "error": str(exc)})
        return observed

    def describe(self) -> Dict[str, Any]:
        return {"kind": DEADLINE_RATIO_KPI, "hosts": dict(self.hosts),
                "sourcePath": self.source_path, "logPath": self.log_path,
                "sessionId": self.session_id, "flowId": self.flow_id,
                "deadlinesMs": list(self.deadlines_ms), "maxAgeMs": self.max_age_ms}


@dataclass
class LiveFlowGoodputObserver(TaggedEchoObserver):
    """Per-flow payload deltas, with tun counters retained as raw diagnostics."""

    source_path: str = ""
    session_id: str = ""
    flow_id: str = ""
    max_age_ms: float = 1500.0
    previous: Dict[str, Any] = field(default_factory=dict)
    last_source: Dict[str, Any] = field(default_factory=dict)
    source_samples: List[Dict[str, Any]] = field(default_factory=list)
    #: The source-clock interval each value of the LAST :meth:`sample` spans.
    intervals: Dict[str, Any] = field(default_factory=dict)
    #: ``{ue: observedAtMs}`` where the current payload stall began (see ``sample``).
    stall_since: Dict[str, float] = field(default_factory=dict)
    #: ``{ue: payloadBytes}`` when the stall began; any delivery past it ends the stall,
    #: even on a poll that only re-establishes the rate baseline after a gap (Codex).
    stall_payload: Dict[str, int] = field(default_factory=dict)
    #: Head-of-line stalls counted as 0 -- data, not failures (``failures`` rows are excised).
    hol_stalls: List[Dict[str, Any]] = field(default_factory=list)

    def command(self, alias: str) -> List[str]:
        command = shlex.join([
            "python3", self.source_path, "snapshot", "--log", self.log_path,
            "--session-id", self.session_id, "--flow-id", self.flow_id,
            "--max-age-ms", str(self.max_age_ms)])
        return ["ssh", "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={int(self.connect_timeout_s)}", alias, command]

    def _snapshot(self, ue: str, alias: str, lines: Any = None) -> Optional[Dict[str, Any]]:
        if lines is None:
            lines = self._lines(ue, alias)
        if lines is None:
            self.previous.pop(ue, None)
            return None
        try:
            row = flow_goodput.validate_snapshot(
                json.loads("\n".join(lines)), self.session_id, self.flow_id, self.max_age_ms)
            identity = (row['clockId'], row['interface'], row['connection'])
            epoch = int(row.get('bindEpoch', 0))
            before = self.last_source.get(ue)
            # A re-registered UE's sink rebinds under the same log and boot with a
            # higher bind epoch: the new tun's counter starts afresh, so no rate is
            # carried across it -- the interval is a gap, recorded as one, never zero.
            rebound = (bool(before) and epoch > before[4] and row['clockId'] == before[0][0]
                       and row['observedAtMs'] > before[1])
            if before and not rebound and (identity != before[0] or row['observedAtMs'] <= before[1]
                                           or row['payloadBytes'] < before[2] or row['tunRxBytes'] < before[3]):
                raise ValueError("changed flow source, reset counter or nonadvancing heartbeat")
            if rebound:
                self.previous.pop(ue, None)
                self.failures.append({"ueId": ue, "host": alias, "error": "source-rebind",
                                      "bindEpoch": epoch})
            self.last_source[ue] = (identity, row['observedAtMs'], row['payloadBytes'], row['tunRxBytes'], epoch)
            self.source_samples.append(dict(row, ueId=ue))
            return row
        except (ValueError, TypeError, KeyError) as exc:
            self.previous.pop(ue, None)
            self.failures.append({"ueId": ue, "host": alias, "error": str(exc)})
            return None

    def _snapshot_missing(self, ue: str) -> None:
        """A host whose read failed: exactly what ``_snapshot`` does when ``_lines`` is None."""
        self.previous.pop(ue, None)
        return None

    def preflight(self) -> Dict[str, Any]:
        """Prove a fresh running source and establish, not invent, a baseline."""
        observed = {}
        for ue, alias in self.hosts.items():
            row = self._snapshot(ue, alias)
            if row is not None:
                self.previous[ue] = row
                observed[f"flowGoodputSource@{ue}"] = row
        return observed

    def sample(self) -> Dict[str, Any]:
        observed = {}
        self.intervals = {}
        read = self._lines_by_ue()
        for ue, alias in self.hosts.items():
            row = self._snapshot(ue, alias, read.get(ue)) if read.get(ue) is not None else self._snapshot_missing(ue)
            if row is None:
                continue
            before = self.previous.get(ue)
            self.previous[ue] = row
            if ue in self.stall_since and row['payloadBytes'] > self.stall_payload.get(ue, 0):
                self.stall_since.pop(ue, None)
            if before is None:
                continue
            elapsed_ms = row['observedAtMs'] - before['observedAtMs']
            payload_delta = row['payloadBytes'] - before['payloadBytes']
            tun_delta = row['tunRxBytes'] - before['tunRxBytes']
            # 2026-09-22 실측: 판 도중 UE 가 재부착하면 tun 이름(oaitun_ue1)은 그대로라
            # 위의 신원 검사를 통과하고, 수신기 프로세스가 살아 있으니 하트비트도 계속
            # 오른다.  바뀌는 것은 주소뿐이고, 송신기는 낡은 주소로 쏜다.  그러면 payload
            # 만 안 늘어 `dlGoodputMbps@ue3 = 0.00` 이 coverage 1.0 으로 **정상값처럼**
            # 기록됐다 -- 그 UE 는 실제로 5 Mbps 를 받고 있었고 같은 창의 마감 성공률은
            # 0.30 이었다.  우리 부하원이 사라진 것을 "UE 가 0 을 받았다" 로 적은 것이다.
            # 판별자는 이미 데이터에 있다: tun 은 받는데 우리 flow 의 payload 만 0 이면
            # 그것은 네트워크가 아니라 우리 송신기가 딴 곳을 향하고 있다는 뜻이다.
            # 그 창은 0 이 아니라 **간극**이므로 KPI 를 내지 않는다(UNKNOWN).
            # 2026-09-27 v5.2 (boards 802-812, 7 windows void on dlGoodputMbps@ue3): with the
            # source identity unchanged -- same address, same TCP connection, checked above --
            # a stall is TCP head-of-line blocking: ue3 under a PRB cap kept receiving ~250 kB
            # per 0.5 s on the tun, the application consumed nothing for 4-8 s, then 2.1-2.3 MB
            # at once.  Those intervals are true application goodput 0 followed by the catch-up,
            # and the window mean over them is the delivered rate; calling them gaps voided the
            # window.  A stall longer than _HOL_STALL_MAX_MS is still the gap below.
            stalled = payload_delta <= 0 and tun_delta > _TUN_ADVANCE_WITHOUT_FLOW_BYTES
            if not stalled and payload_delta > 0:
                self.stall_since.pop(ue, None)     # only delivery ends a stall; the clock persists across gaps
            if stalled and ue not in self.stall_since:
                self.stall_since[ue] = before['observedAtMs']
                self.stall_payload[ue] = before['payloadBytes']
            since = self.stall_since.get(ue) if stalled else None
            if stalled and row['observedAtMs'] - since <= _HOL_STALL_MAX_MS:
                self.hol_stalls.append({"ueId": ue, "atMs": row['observedAtMs'],
                                        "stallSinceMs": since, "tunDeltaBytes": tun_delta})
                stalled = False
            if stalled:
                self.previous.pop(ue, None)
                self.failures.append({"ueId": ue, "host": self.hosts.get(ue),
                                      # 이 항목은 관측원 **끊김**이 아니다.  다른 관측원 공백은
                                      # 우리 측 중단으로 도려내지만(`agent._our_side_gap`),
                                      # 이것은 종류를 밝혀 도려내기에서 뺀다 -- 제어가 flow 를
                                      # 굶긴 결과일 수 있으므로 방법의 결과로 남아야 한다.
                                      "kind": "flow-payload-mismatch",
                                      # codex 감사 2026-09-22: 이것은 **불일치 탐지**이지
                                      # 원인 증명이 아니다.  낡은 주소일 수도, 송신 중단일
                                      # 수도, 다른 흐름만 흐르는 것일 수도 있다.  사실만 적는다.
                                      "error": "flow payload did not advance while the tun"
                                               f" received +{tun_delta} B: this window cannot"
                                               " be read as the UE receiving nothing",
                                      "tunDeltaBytes": tun_delta})
                continue
            rate = payload_delta * 8 / 1000 / elapsed_ms
            observed[f"{GOODPUT_KPI}@{ue}"] = rate
            observed[f"{LOW_DELIVERY_RUN_KPI}@{ue}"] = rate
            interval = {"startMs": before['observedAtMs'], "endMs": row['observedAtMs'],
                        "clockId": row['clockId']}
            self.intervals[f"{GOODPUT_KPI}@{ue}"] = interval
            self.intervals[f"{LOW_DELIVERY_RUN_KPI}@{ue}"] = dict(interval)
        return observed

    def describe(self) -> Dict[str, Any]:
        return {"kind": GOODPUT_KPI, "measurementDefinition": flow_goodput.MEASUREMENT,
                "hosts": dict(self.hosts), "sourcePath": self.source_path, "logPath": self.log_path,
                "sessionId": self.session_id, "flowId": self.flow_id, "maxAgeMs": self.max_age_ms,
                "tunCounterRole": "operational-cross-check-not-application-goodput",
                "holStalls": list(self.hol_stalls),
                "sourceSamples": list(self.source_samples)}


def cell_goodput_totals(observed: Mapping[str, Any]) -> Dict[str, float]:
    """``cellGoodputMbps@<nci>`` -- 그 셀에 붙어 있는 UE 들의 goodput 합.

    **한 UE 라도 그 창에서 goodput 이나 소속 셀을 못 읽었으면 아무 셀도 내지 않는다.**
    빠진 UE 를 0 으로 치면 셀 총량이 조용히 작아지고, 그 판은 "사업자 요구가 안 지켜졌다"
    로 읽힌다 -- 측정 공백을 위반으로 세탁하는 것이다.  합계는 전부 읽혔을 때만 참이다.

    UE 가 한 대도 없는 셀은 키를 내지 않는다.  실제로 0 Mbps 인 것은 맞지만 그 판에서
    우리가 관측한 것은 "그 셀에 대해 아무 표본도 없다" 이므로, 0 을 발행하면 관측하지
    않은 값을 발행하는 것이 된다.  키가 없으면 판정은 UNKNOWN 이 되고 그것이 정직하다.
    """
    cells: Dict[str, str] = {}
    rates: Dict[str, float] = {}
    for key, value in dict(observed or {}).items():
        kind, _, target = str(key).partition('@')
        if not target:
            continue
        if kind == SERVING_CELL_KPI and value not in (None, '', 'UNKNOWN'):
            cells[target] = str(value)
        elif kind == GOODPUT_KPI and isinstance(value, (int, float)):
            rates[target] = float(value)
    if not cells or set(cells) != set(rates):
        return {}
    totals: Dict[str, float] = {}
    for ue, cell in cells.items():
        totals[cell] = totals.get(cell, 0.0) + rates[ue]
    return {f"{CELL_GOODPUT_KPI}@{cell}": total for cell, total in totals.items()}



def cell_attenuations_from_lines(lines: Sequence[Any], adapter: Any, cells: Sequence[int],
                                 topology: Any, max_age_s: Optional[float] = None,
                                 wall_s: Callable[[], float] = time.time) -> Dict[int, str]:
    """``{nci: "<dB>"}`` for each of ``cells`` that has a ``RAN.Cell.TxAttenuationDb``
    sample in ``lines`` (the last one wins); a cell without a sample is absent.

    KPM carries the counter at node scope with no NCI, so the cell is found through
    ``e2_node`` -> nb_id -> the deployment's ``nb_id_to_nci`` (NCI cannot be derived
    from nb_id arithmetically).  Measured 2026-09-18: nb=2816 -> 10.0, nb=3584 -> 0.0.
    """
    if not lines:
        return {}
    try:
        samples = adapter.parse_lines(lines).samples
    except Exception:  # noqa: BLE001 - an unreadable batch is a missing sample
        return {}
    found = _cell_attenuation_samples(samples, cells, topology)
    if max_age_s is None:
        return {cell: value for cell, (value, _at) in found.items()}
    # A baseline is a *current* setting (Codex round 2): a queued record older than
    # ``max_age_s`` may predate a restore and would freeze the wrong baseline.
    from assurance.core.timebase import parse_utc
    now, fresh = float(wall_s()), {}
    for cell, (value, at) in found.items():
        try:
            if 0.0 <= now - parse_utc(at).timestamp() <= float(max_age_s):
                fresh[cell] = value
        except Exception:  # noqa: BLE001 - no readable time, no baseline
            continue
    return fresh


def _cell_attenuation_samples(samples: Sequence[Any], cells: Sequence[int],
                              topology: Any) -> Dict[int, Tuple[str, str]]:
    """``{nci: ("<dB>", observed_at)}`` -- the newest sample per cell *by its own time*
    (the KPM receive time on this host), so a delayed or replayed record never outranks a
    newer one (Codex review 2026-09-25 #1)."""
    from assurance.live.pin_to_cell_driver import kpm_node_nb_id
    by_node: Dict[str, Tuple[str, str]] = {}
    for sample in samples:
        if str(getattr(sample, "counter_id", "")) != KPM_TX_ATTENUATION_COUNTER:
            continue
        node = str((sample.scope_snapshot or {}).get("e2_node", ""))
        at = str(getattr(sample, "observed_at", "") or "")
        if node and (node not in by_node or at >= by_node[node][1]):
            by_node[node] = (str(float(sample.value.value)), at)
    nci_of = dict(getattr(topology, "nb_id_to_nci", {}) or {})
    wanted = {int(c) for c in cells}
    found: Dict[int, Tuple[str, str]] = {}
    for node, value in by_node.items():
        nb = kpm_node_nb_id(node)
        nci = nci_of.get(nb) if nb is not None else None
        if nci is not None and int(nci) in wanted:
            found[int(nci)] = value
    return found


@dataclass
class KpmCellAttenuationObserver:
    """``cellTxAttenuationDb@<nci>`` per poll from the KPM tail.

    Each poll reads only the lines that arrived since the last one, so the last value
    seen per cell is kept with its time and published while it is younger than
    ``max_age_ms``.  A cell with no fresh sample publishes nothing -- UNKNOWN, never a
    declared default (a declared 0.0 against a live 10 dB is how 2026-09-18 lost boards).
    """

    read_new_lines: Callable[[], Sequence[Any]]
    adapter: Any
    topology: Any
    cells: Tuple[int, ...]
    max_age_ms: float = 10000.0
    #: Wall clock in epoch seconds -- the same host clock the KPM receive time is on.
    wall_s: Callable[[], float] = time.time
    _last: Dict[int, Tuple[float, float]] = field(default_factory=dict)

    def sample(self) -> Dict[str, Any]:
        """Age is measured from the record's own receive time, not from when this poll
        dequeued it (Codex review 2026-09-25 #1): a queued pre-action reading must not look
        fresh.  An older record never replaces a newer one."""
        from assurance.core.timebase import parse_utc
        try:
            lines = self.read_new_lines()
        except Exception:  # noqa: BLE001 - a stalled tail is a missing sample
            lines = ()
        samples = ()
        if lines:
            try:
                samples = self.adapter.parse_lines(lines).samples
            except Exception:  # noqa: BLE001 - an unreadable batch is a missing sample
                samples = ()
        for cell, (value, at) in _cell_attenuation_samples(
                samples, self.cells, self.topology).items():
            try:
                source_s = parse_utc(at).timestamp()
            except Exception:  # noqa: BLE001 - a record with no readable time proves nothing
                continue
            if cell not in self._last or source_s >= self._last[cell][0]:
                self._last[cell] = (source_s, float(value))
        now = float(self.wall_s())
        return {f"{CELL_TX_ATTENUATION_KPI}@{cell}": value
                for cell, (at, value) in self._last.items()
                if 0.0 <= now - at <= float(self.max_age_ms) / 1000.0}

@dataclass
class CompositeKpiObserver:
    """Sample the real independent sources; a missing echo cannot hide goodput."""

    observers: Tuple[KpiObserver, ...]

    @property
    def failures(self) -> List[Dict[str, Any]]:
        return [row for observer in self.observers
                for row in getattr(observer, 'failures', ())]

    @property
    def intervals(self) -> Dict[str, Any]:
        merged = {key: value for observer in self.observers
                  for key, value in dict(getattr(observer, 'intervals', None) or {}).items()}
        merged.update(getattr(self, '_cell_intervals', None) or {})
        return merged

    def sample(self) -> Dict[str, Any]:
        observed = {}
        with ThreadPoolExecutor(max_workers=max(1, len(self.observers))) as pool:
            pending = [pool.submit(observer.sample) for observer in self.observers]
            for result in pending:
                values = dict(result.result())
                if set(observed).intersection(values):
                    raise KpiObserverError("multiple sources answer the same KPI key")
                observed.update(values)
        # 집계는 **맨 마지막에** 얹는다 -- 모든 관측원이 답한 뒤라야 "한 UE 가 빠졌다" 를
        # 알 수 있다.  중간에 더하면 아직 안 온 UE 를 없는 것으로 세게 된다.
        totals = cell_goodput_totals(observed)
        if set(observed).intersection(totals):
            raise KpiObserverError("multiple sources answer the same KPI key")
        observed.update(totals)
        self._cell_intervals = {
            key: _common_interval(self.intervals, key.split('@', 1)[1], observed)
            for key in totals}
        return observed

    def include_contract_deadlines(self, contract: Any) -> None:
        for observer in self.observers:
            if not isinstance(observer, LiveTaggedEchoObserver):
                continue
            values = set(observer.deadlines_ms)
            for req_id, entry in contract.authorization.requirements.items():
                if entry.kpi != DEADLINE_RATIO_KPI or entry.scope.split('@')[-1] not in observer.hosts:
                    continue
                values.update(float(target.deadline_for(req_id) or entry.deadline_ms)
                              for target in contract.targets)
            observer.deadlines_ms = tuple(sorted(values))

    def describe(self) -> Dict[str, Any]:
        return {"sources": [observer.describe() if hasattr(observer, 'describe') else
                            {"kind": type(observer).__name__,
                             "hosts": dict(getattr(observer, 'hosts', {}))}
                            for observer in self.observers]}


def _common_interval(intervals: Mapping[str, Any], cell: str,
                     observed: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """합계가 유효한 구간 = 기여한 UE 구간들의 **교집합**.

    가장 늦게 시작하고 가장 일찍 끝난 UE 가 합계의 창을 정한다.  합집합을 쓰면 어떤
    UE 도 관측되지 않은 시각을 합계가 덮는다고 주장하게 된다.
    """
    members = [ue for key, value in dict(observed or {}).items()
               for kind, _, ue in [str(key).partition('@')]
               if kind == SERVING_CELL_KPI and str(value) == cell]
    rows = [dict(intervals.get(f"{GOODPUT_KPI}@{ue}") or {}) for ue in members]
    rows = [row for row in rows if row.get('startMs') is not None and row.get('endMs') is not None]
    if not rows or len({row.get('clockId') for row in rows}) != 1:
        return None
    start, end = max(r['startMs'] for r in rows), min(r['endMs'] for r in rows)
    if end <= start:
        return None
    return {"startMs": start, "endMs": end, "clockId": rows[0].get('clockId')}


def live_echo_sources(kpi_keys: Sequence[str], *, profile_document: Mapping[str, Any],
                      hosts: Mapping[str, str]) -> Dict[str, Dict[str, Any]]:
    """Validate explicit per-UE source descriptors without contacting a host."""
    configured = dict(dict(profile_document.get('liveConsole') or {}).get('taggedEcho') or {})
    sources = {}
    for key in kpi_keys:
        kind, _, ue = str(key).partition('@')
        if kind != DEADLINE_RATIO_KPI:
            continue
        source = dict(configured.get(ue) or {})
        if not source:
            refuse_unobserved_kpis([key])
        if (ue not in hosts or ue_hosts_from_profile(profile_document).get(ue) != hosts[ue]
                or any(not str(source.get(field, '')).strip()
                       for field in ('sourcePath', 'logPath', 'sessionId', 'flowId'))):
            raise KpiObserverError(f"{key}: explicit UE host and taggedEcho sourcePath, "
                                   "logPath, sessionId and flowId are required")
        age = _number(source.get('maxAgeMs', 1500))
        if age is None or not math.isfinite(age) or not 0 < age <= 10000:
            raise KpiObserverError(f"{key}: taggedEcho maxAgeMs must be in (0, 10000]")
        sources[ue] = source
    return sources


def live_flow_sources(kpi_keys: Sequence[str], *, profile_document: Mapping[str, Any],
                      hosts: Mapping[str, str]) -> Dict[str, Dict[str, Any]]:
    """Explicit application sources opt in; absent sources retain legacy tun KPI semantics."""
    configured = dict(profile_document.get('liveConsole') or {}).get('flowGoodput', {})
    if not isinstance(configured, Mapping):
        raise KpiObserverError("liveConsole.flowGoodput must map UE IDs to source descriptors")
    sources = {}
    for key in kpi_keys:
        kind, _, ue = str(key).partition('@')
        if kind != GOODPUT_KPI or ue not in configured:
            continue
        if not isinstance(configured[ue], Mapping):
            raise KpiObserverError(f"{key}: flowGoodput source must be an object")
        source = dict(configured[ue])
        if (ue not in hosts or ue_hosts_from_profile(profile_document).get(ue) != hosts[ue]
                or any(not isinstance(source.get(field), str) or not source[field].strip()
                       for field in ('sourcePath', 'logPath', 'sessionId', 'flowId'))):
            raise KpiObserverError(f"{key}: explicit UE host and flowGoodput sourcePath, "
                                   "logPath, sessionId and flowId are required")
        age = _number(source.get('maxAgeMs', 1500))
        if age is None or not math.isfinite(age) or not 0 < age <= 10000:
            raise KpiObserverError(f"{key}: flowGoodput maxAgeMs must be in (0, 10000]")
        sources[ue] = source
    return sources


def build_profile_observer(requirements: Sequence[Any], *,
                           profile_document: Mapping[str, Any], hosts: Mapping[str, str],
                           serving_cells: Optional[Callable[[], Mapping[str, str]]] = None,
                           **kwargs: Any) -> KpiObserver:
    """The real LIVE composition selected from the operator's deployment profile."""
    keys = [requirement.observation_key for requirement in requirements]
    sources = live_echo_sources(keys, profile_document=profile_document, hosts=hosts)
    flow_sources = live_flow_sources(keys, profile_document=profile_document, hosts=hosts)
    deadlines: Dict[str, set] = {}
    for req in requirements:
        if req.kpi == DEADLINE_RATIO_KPI:
            levels = tuple(req.deadline_levels)
            deadlines.setdefault(req.scope.split('@')[-1], set()).update(levels)
            if len(levels) > 1:
                # v4.7 paper metric (V47_FINAL_PLAN 3.3): the evaluator judges the deadline
                # axis at 1/20 of its span from the same request records.  The runtime ladder
                # above is kept; these are only extra points the same counters are read at.
                lo, hi = float(levels[0]), float(levels[-1])
                deadlines[req.scope.split('@')[-1]].update(
                    round(lo + (hi - lo) * q / 20, 9) for q in range(21))
    observers = []
    for ue, source in sources.items():
        values = tuple(sorted(float(d) for d in deadlines.get(ue, ())))
        if not values or any(not math.isfinite(d) or d <= 0 for d in values):
            raise KpiObserverError(f"{DEADLINE_RATIO_KPI}@{ue}: no valid deadline")
        observers.append(LiveTaggedEchoObserver(
            hosts={ue: hosts[ue]}, source_path=source['sourcePath'],
            log_path=source['logPath'], session_id=source['sessionId'], flow_id=source['flowId'],
            deadlines_ms=values, max_age_ms=float(source.get('maxAgeMs', 1500)), **kwargs))
    for ue, source in flow_sources.items():
        observers.append(LiveFlowGoodputObserver(
            hosts={ue: hosts[ue]}, source_path=source['sourcePath'],
            log_path=source['logPath'], session_id=source['sessionId'], flow_id=source['flowId'],
            max_age_ms=float(source.get('maxAgeMs', 1500)), **kwargs))
    # 2026-09-22 (codex 감사): flow source 가 없는 UE 는 조용히 tun 집계로 대체되는데,
    # 둘 다 같은 `dlGoodputMbps@UE` 이름을 쓴다.  application flow 는 우리 송신원이 보낸
    # 바이트를, tun 집계는 그 UE 가 받은 **모든** 트래픽을 센다.  한 판 안에서 UE 마다
    # 정의가 다르면 같은 목표에 서로 다른 것을 비교하게 된다.  구성 전체가 legacy(아무도
    # flow source 가 없음)면 종전대로 두고, **섞인 구성만** 거절한다.
    tun_only = [ue for ue in hosts if ue not in flow_sources]
    if flow_sources and tun_only:
        raise KpiObserverError(
            "liveConsole.flowGoodput names an application source for "
            f"{sorted(flow_sources)} but not for {sorted(tun_only)}; the same "
            f"{GOODPUT_KPI}@UE key would then carry two different measurement "
            "definitions in one sitting. Configure every UE or none.")
    tun = TunRateObserver(hosts={ue: host for ue, host in hosts.items() if ue not in flow_sources},
                          serving_cells=serving_cells, verify_interface=True, **kwargs)
    return CompositeKpiObserver((tun, *observers)) if observers else tun


def _echo_row(line: Any) -> Optional[Dict[str, Any]]:
    text = str(line or "").strip()
    if not text:
        return None
    try:
        row = json.loads(text)
    except ValueError:
        return None
    return dict(row) if isinstance(row, Mapping) else None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# what this deployment cannot observe
# --------------------------------------------------------------------------- #


def refuse_unobserved_kpis(kpi_keys: Sequence[str], *,
                           observers: Mapping[str, Any] = (),  # type: ignore[assignment]
                           ) -> None:
    """Refuse, **by name**, every KPI this deployment cannot measure.

    The KPI half of ``tools/liveconsole/agent.py``'s ``_refuse_uncarried_axes``
    and deliberately the same shape: the kind, why nothing here can measure it,
    and the two ways forward.  An observer injected for that kind answers the
    question on its own and the refusal is skipped -- which is exactly what
    makes the hardware-free runtime the same composition with different ports,
    and is *not* a licence to inject the emulator into a live sitting.
    """
    injected = dict(observers or {})
    refusals: List[str] = []
    for key in kpi_keys or ():
        kind = str(key or "").split("@", 1)[0]
        reason = LIVE_UNOBSERVED_KPIS.get(kind)
        if not reason or kind in injected:
            continue
        message = (f"the {key} target cannot be judged on this deployment: "
                   f"{reason}.  Drop the intent, or compose the sitting over a "
                   f"KPI observer for {kind}.")
        if message not in refusals:
            refusals.append(message)
    if refusals:
        raise KpiObserverError(" ".join(refusals))


def build_live_observer(kpi_keys: Sequence[str], *, hosts: Mapping[str, str],
                        serving_cells: Optional[Callable[[], Mapping[str, str]]] = None,
                        observers: Mapping[str, Any] = (),  # type: ignore[assignment]
                        **kwargs: Any) -> KpiObserver:
    """The observer a live sitting judges its targets with, or a refusal.

    One place the live composition asks "can every KPI in ``T`` be measured
    here?", so a KPI nobody can read is refused before the sitting starts
    rather than falling out of the vector as ``UNKNOWN`` at the end of the
    first hold -- an unmeasurable target and a target that merely missed a
    window are different facts and the operator is owed the difference.
    """
    refuse_unobserved_kpis(kpi_keys, observers=observers)
    tun = TunRateObserver(hosts=hosts, serving_cells=serving_cells, **kwargs)
    added = tuple(dict(observers or {}).values())
    if any(not callable(getattr(observer, 'sample', None)) for observer in added):
        raise KpiObserverError("a KPI observer must implement sample()")
    return CompositeKpiObserver((tun, *added)) if added else tun
