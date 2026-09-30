"""Fail-closed Campaign-5 A1 policy worker for ``our_rc_xapp``.

The worker deliberately reuses the operator-lane ``fire_xapp.sh`` wrapper.  It
does not encode E2SM-RC itself: it resolves a fresh UE control header from the
same KPM/header material as ``refresh_headers.sh``, supplies the wrapper's
closed ``RC_*`` vocabulary, and treats the xApp ACK only as delivery evidence.
Effect evidence is a newer, same-epoch KPM configuration counter.
"""

from __future__ import annotations

import copy
import fcntl
import json
import logging
import os
import re
import subprocess
import tempfile
from collections import deque
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence, Union

from assurance.gateway.power_spacing import (mark_power_write, power_write_wait_s,
                                            seconds_since_power_write)

#: A cell reading counts as current only this long after the last RF write (Codex review).
POWER_SKIP_AFTER_S = 10.0
from oran.contract.jcs import jcs_sha256

from ._locking import serialized
from .families import Campaign5Error, Campaign5Family, family_by_policy_type

SLICE_POLICY_TYPE_ID = "AIC_SliceSLATarget_1.0.0"
_LOG = logging.getLogger(__name__)

__all__ = [
    "ControlOutcome",
    "IdentityRefusal",
    "LiveWorkerError",
    "LiveWorkerRefusal",
    "Campaign5LiveWorker",
    "Campaign5WorkerDispatcher",
    "SLICE_POLICY_TYPE_ID",
]


class LiveWorkerError(RuntimeError):
    """The live worker could not safely complete an operation."""


class LiveWorkerRefusal(LiveWorkerError):
    """A pre-write guard refused the operation; no control was sent."""


class IdentityRefusal(LiveWorkerRefusal):
    """The policy scope has no fresh, unambiguous KPM-to-header binding."""


class ScopeConflict(LiveWorkerRefusal):
    """Another durable policy owns this UE/action writer slot."""


@dataclass(frozen=True)
class ControlOutcome:
    """One policy attempt, separating control delivery from observed effect."""

    control_ack: bool
    effect_verified: bool
    observed_config: Optional[Mapping[str, Any]]
    rollback_attempted: bool
    rollback_verified: bool
    detail: str


@dataclass(frozen=True)
class _Action:
    policy_type_id: str
    action_id: int
    counter: str
    value_fields: tuple[str, ...]
    env_fields: Mapping[str, str]
    ue_scoped: bool


@dataclass(frozen=True)
class _Identity:
    ue_tag: str
    amf_ue_ngap_id: int
    ran_ue_id: int
    guami: Mapping[str, int]
    epoch: int


@dataclass(frozen=True)
class _CellIdentity:
    """Durable cell scope, deliberately independent of any attached UE."""

    cell_id: str
    nb_id: int
    epoch: int


_ScopeIdentity = Union[_Identity, _CellIdentity]


_ACTIONS: dict[str, _Action] = {
    "AIC_UeDlPrbCap_1.0.0": _Action(
        "AIC_UeDlPrbCap_1.0.0", 102, "RAN.UE.DlPrbCap",
        ("maxDlPrbs",), {"maxDlPrbs": "RC_CAP_MAX_DL_PRBS"}, True,
    ),
    "AIC_SchedulerPriority_1.0.0": _Action(
        "AIC_SchedulerPriority_1.0.0", 103, "RAN.UE.PfWeight",
        ("pfWeight",), {"pfWeight": "RC_PF_WEIGHT"}, True,
    ),
    "AIC_DlMcsBounds_1.0.0": _Action(
        "AIC_DlMcsBounds_1.0.0", 101, "RAN.Cell.DlMcsBounds",
        ("minDlMcs", "maxDlMcs"),
        {"minDlMcs": "RC_MCS_MIN", "maxDlMcs": "RC_MCS_MAX"}, False,
    ),
    "AIC_CellDlTxPower_1.0.0": _Action(
        "AIC_CellDlTxPower_1.0.0", 104, "RAN.Cell.TxAttenuationDb",
        ("txAttenuationDb",), {"txAttenuationDb": "RC_TX_ATTEN_DB"}, False,
    ),
    SLICE_POLICY_TYPE_ID: _Action(
        SLICE_POLICY_TYPE_ID, 6, "RAN.SlicePrbQuotaMin",
        ("minPrbPolicyRatio", "maxPrbPolicyRatio", "dedicatedPrbPolicyRatio"),
        {
            "minPrbPolicyRatio": "RC_SLICE_MIN_RATIO",
            "maxPrbPolicyRatio": "RC_SLICE_MAX_RATIO",
            "dedicatedPrbPolicyRatio": "RC_SLICE_DEDICATED_RATIO",
        },
        False,
    ),
}

_HEADER_KEYS = (
    "RC_HEADER_RRC_UE_ID",
    "RC_HEADER_AMF_UE_NGAP_ID",
    "RC_UE_GUAMI_MCC",
    "RC_UE_GUAMI_MNC",
    "RC_UE_GUAMI_MNC_LEN",
    "RC_UE_AMF_REGION_ID",
    "RC_UE_AMF_SET_ID",
    "RC_UE_AMF_POINTER",
)
_GUAMI_KEYS = {
    "mcc": "RC_UE_GUAMI_MCC",
    "mnc": "RC_UE_GUAMI_MNC",
    "mnc_digit_len": "RC_UE_GUAMI_MNC_LEN",
    "amf_region_id": "RC_UE_AMF_REGION_ID",
    "amf_set_id": "RC_UE_AMF_SET_ID",
    "amf_pointer": "RC_UE_AMF_POINTER",
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _same_number(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    return left == right


class _JsonLedger:
    """Small atomic ledger; its lock also serializes writers across processes."""

    def __init__(self, path: str | os.PathLike[str], *, binding: Mapping[str, Any]) -> None:
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        self.binding = dict(binding)
        self._thread_lock = threading.RLock()

    def snapshot(self) -> dict[str, Any]:
        with self.edit(read_only=True) as state:
            return copy.deepcopy(state)

    @contextmanager
    def edit(self, *, read_only: bool = False) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state: dict[str, Any] = {"version": 1, "entries": {}, "owners": {}}
            if self.path.exists():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise LiveWorkerError("live worker ledger is unreadable") from exc
                if not isinstance(loaded, dict) or loaded.get("version") != 1:
                    raise LiveWorkerError("live worker ledger has an unsupported format")
                state.update(loaded)
            if not isinstance(state["entries"], dict) or not isinstance(state["owners"], dict):
                raise LiveWorkerError("live worker ledger entries/owners are malformed")
            binding = state.get("deploymentBinding")
            if binding is not None and binding != self.binding:
                raise LiveWorkerError("ledger deployment binding differs from configured cell/node")
            # New ledgers are pinned. Populated legacy ledgers remain usable in
            # the old single-cell lane, without inventing their missing node.
            if (not read_only and binding is None
                    and not state["entries"] and not state["owners"]):
                state["deploymentBinding"] = dict(self.binding)
            try:
                yield state
                if not read_only:
                    self._write(state)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write(self, state: Mapping[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def flush(self, state: Mapping[str, Any]) -> None:
        """Persist an in-lock crash boundary before an external write."""
        self._write(state)


def _kpm_node_nb_id(node: Any) -> Optional[int]:
    # Parse the gate's canonical node key locally: the O-RAN layer must not
    # import assurance. Membership of node and record nb_id is one binding.
    if not isinstance(node, str):
        return None
    fields = [part[3:] for part in node.split(";") if part.startswith("nb=")]
    if len(fields) != 1 or re.fullmatch(r"[0-9]+/[0-9]+", fields[0]) is None:
        return None
    try:
        return int(fields[0].split("/", 1)[0])
    except ValueError:
        return None


#: How many KPM lines the gate keeps in memory.
#:
#: Every reader of this cache wants **recent** lines: two call sites filter the
#: whole history through ``fresh()`` (freshness is 5 s in the live deployment) and
#: the third walks backwards from the newest.  Keeping every line ever read was
#: therefore pure ballast, and on 2026-09-17 the live action producer sat at
#: **1060 MB RSS** with the stream at 178 MB / 177,298 lines and still growing at
#: 5 KB/s -- an unbounded leak with an OOM at the end of it.  The 2026-09-15 fix
#: stopped re-reading the file; it did not stop remembering it.
#:
#: At the observed ~6 lines/s this window is about 55 minutes, which is seven
#: sittings -- far more than any ``min_line`` marker, taken at the start of one
#: write, can need.
KPM_LINE_WINDOW = 20000


class _KpmGate:
    def __init__(self, path: Path, *, clock: Callable[[], datetime],
                 freshness_s: float, nb_id: int) -> None:
        self.path = path
        self.clock = clock
        self.freshness_s = float(freshness_s)
        self.nb_id = int(nb_id)
        self._lock = threading.Lock()

    def _lines(self) -> list[str]:
        """The file's complete lines, read incrementally.

        Re-reading the whole JSONL on every call (every 0.1 s while an apply is
        verified, every second per retained expiry) pinned the live producer at
        100 % CPU once the stream passed 35 MB and slowed its A1 answers to
        5-8 s (2026-09-15).  A replaced or truncated file starts over.
        """
        with self._lock:
            return self._read_new_lines()

    def _read_new_lines(self) -> list[str]:
        try:
            stat = self.path.stat()
        except OSError:
            return []
        cache = getattr(self, "_cache", None)
        if cache is None or cache["inode"] != stat.st_ino or stat.st_size < cache["offset"]:
            cache = {"inode": stat.st_ino, "offset": 0,
                     "lines": deque(maxlen=KPM_LINE_WINDOW), "dropped": 0,
                     "partial": b""}
            self._cache = cache
        if stat.st_size > cache["offset"]:
            try:
                with self.path.open("rb") as handle:
                    handle.seek(cache["offset"])
                    chunk = handle.read(stat.st_size - cache["offset"])
            except OSError:
                return list(cache["lines"])
            cache["offset"] += len(chunk)
            data = cache["partial"] + chunk
            *complete, cache["partial"] = data.split(b"\n")
            before = len(cache["lines"])
            cache["lines"].extend(line.decode("utf-8", "replace") for line in complete)
            # The deque drops from the left; line numbers stay absolute so a
            # ``min_line`` marker taken before the eviction still means the line
            # it meant then.
            cache["dropped"] += before + len(complete) - len(cache["lines"])
        return cache["lines"]

    def _dropped(self) -> int:
        return int((getattr(self, "_cache", None) or {}).get("dropped", 0))

    def line_count(self) -> int:
        """Absolute count, including lines the window has already dropped."""
        lines = self._lines()
        return self._dropped() + len(lines)

    def records(self, *, min_line: int = 0) -> list[tuple[int, Mapping[str, Any]]]:
        lines = list(self._lines())
        first = self._dropped()
        records: list[tuple[int, Mapping[str, Any]]] = []
        for index in range(max(0, min_line - first), len(lines)):
            line_no = first + index
            line = lines[index]
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(value, Mapping) and value.get("event") == "kpm_indication":
                records.append((line_no, value))
        return records

    def fresh(self, record: Mapping[str, Any]) -> bool:
        received_us = _as_int(record.get("recv_unix_us"))
        if (received_us is None or _as_int(record.get("nb_id")) != self.nb_id
                or _kpm_node_nb_id(record.get("e2_node")) != self.nb_id):
            return False
        age = self.clock().timestamp() - received_us / 1_000_000
        return -5.0 <= age <= self.freshness_s


class Campaign5LiveWorker:
    """Durable, single-writer A1 policy worker using the proven RC wrapper."""

    def __init__(
        self,
        *,
        kpm_jsonl: str | os.PathLike[str],
        header_dir: str | os.PathLike[str],
        fire_xapp: str | os.PathLike[str],
        ledger_path: str | os.PathLike[str],
        cell_id: str,
        nb_id: int,
        freshness_s: float = 5.0,
        deadline_s: float = 18.0,
        poll_interval_s: float = 0.1,
        clock: Callable[[], datetime] = _now_utc,
        runner: Callable[..., Any] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        cell_ue_wait_s: float = 0.0,
    ) -> None:
        if not cell_id:
            raise ValueError("cell_id must be a non-empty deployment binding")
        if freshness_s <= 0 or deadline_s <= 0 or poll_interval_s <= 0:
            raise ValueError("freshness, deadline and poll interval must be positive")
        self._headers = Path(header_dir)
        self._fire = Path(fire_xapp)
        self._cell_id = str(cell_id)
        self._nb_id = int(nb_id)
        self._clock = clock
        self._runner = runner
        self._sleep = sleep
        self._cell_ue_wait_s = max(0.0, float(cell_ue_wait_s))
        self._deadline_s = float(deadline_s)
        self._poll_s = float(poll_interval_s)
        self._gate = _KpmGate(
            Path(kpm_jsonl), clock=clock, freshness_s=freshness_s, nb_id=nb_id
        )
        self._ledger = _JsonLedger(
            ledger_path, binding={"cellId": self._cell_id, "nbId": self._nb_id}
        )
        self._lifecycle_lock = threading.RLock()
        self._producer: Any = None
        self._delete_capability: Any = None

    # -- producer binding -------------------------------------------------

    def bind(self, producer: Any) -> None:
        """Bind exactly once to a Campaign5 producer's PUT/DELETE lifecycle."""
        with producer.lifecycle_lock:
            self._check_attachment(producer)
            self._ledger.snapshot()  # Refuse an explicitly mismatched binding before recovery.
            capability = producer.bind_live_worker(self._apply_from_a1, self._delete_from_a1)
            self._attach(producer, capability)
        # Outside the registry lock: recovery re-enters ``put_policy``, which
        # takes the cell lock first (lock order: cell, ledger, registry).
        self._recover_producer_view()

    def _check_attachment(self, producer: Any) -> None:
        if self._producer is not None and self._producer is not producer:
            raise LiveWorkerError("live worker is already bound to another producer")

    def _attach(self, producer: Any, capability: object) -> None:
        self._check_attachment(producer)
        self._producer = producer
        self._delete_capability = capability
        # This cell's writer lock, shared with the producer's PUT/DELETE for
        # the same cell -- not the producer-wide lock, which used to stall
        # every cell behind one cell's 18 s control (2026-09-23 audit).
        self._lifecycle_lock = producer.cell_lock(self._cell_id)

    def _recover_producer_view(self) -> None:
        """Rehydrate active A1 resources and reconcile without re-sending apply."""
        assert self._producer is not None
        with self._ledger.edit() as state:
            recoverable: list[tuple[str, str, Mapping[str, Any]]] = []
            for policy_id, entry in state["entries"].items():
                if state["owners"].get(entry.get("scopeKey")) != policy_id:
                    continue
                digest = entry.get("currentDigest")
                attempt = entry.get("attempts", {}).get(digest)
                if not isinstance(attempt, Mapping):
                    continue
                policy = attempt.get("policy")
                policy_type_id = entry.get("policyTypeId")
                if (isinstance(policy, Mapping) and isinstance(policy_type_id, str)
                        and policy_type_id in self._producer.get_policytypes()):
                    recoverable.append((policy_type_id, policy_id, copy.deepcopy(policy)))
        for policy_type_id, policy_id, policy in recoverable:
            # put_policy invokes this worker.  The durable digest already
            # exists, so reconciliation may observe or restore but never send
            # the requested write a second time.
            self._producer.put_policy(policy_type_id, policy_id, policy)

    def _apply_from_a1(self, policy_type_id: str, policy_id: str,
                       policy: Mapping[str, Any]) -> None:
        assert self._producer is not None
        try:
            outcome = self.apply(policy_type_id, policy_id, policy)
        except Exception as exc:                  # noqa: BLE001 - 아래에서 갈라 낸다
            # `LiveWorkerError` 만 잡으면 **원장 저장 실패가 이 판별자를 통째로 건너뛴다.**
            # `_write()` 의 fsync·replace 는 `OSError` 를 던지는데, 그것은 라디오를 이미
            # 때린 **뒤** 두 번째 flush 에서 날 수 있다.  그러면 예외가 그대로 올라가
            # 프로듀서가 생성을 되감고 dispatcher 도 소유자를 지워, **라디오는 바뀐 값을
            # 들고 있는데 그것을 책임질 정책이 아무 데도 없는 상태**가 된다
            # (2026-09-21 codex 재현: radio 6 · ledger owes True · producerPolicies []).
            # 예외의 **종류**가 아니라 원장이 말하는 **책임 유무**가 갈림이어야 한다.
            #
            # 여기서 조용히 돌아가면 프로듀서는 **적용이 끝난 것으로 보고** scope 주장을
            # 유지한다.  프로듀서의 되감기는 이 핸들러가 raise 할 때만 돈다
            # (`producer.py:314`).  삼킨 결과, 워커가 **원장에 아무것도 쓰기 전에**
            # 거절한 정책(신원 거절·기준선 부재·scope 충돌)이 영구히 축을 점유했다:
            # 2026-09-17 실측으로 아홉 정책이 살아 있었고 **한 건도** 원장에 없었으며,
            # 그중 하나가 네 판을 연달아 거절하고 그중 셋이 `RECOVERY_FAILURE` 로 끝났다.
            #
            # 판별자는 `entries` 가 **아니다**.  정상 롤백·DELETE 는 `entries[id]` 를
            # 남기고 `owners` 만 지우므로, 같은 id 를 재사용하면 끝난 이력이 "빚" 으로
            # 읽혀 같은 영구 점유가 되살아난다 (2026-09-21 codex 3회차 재현).
            # `expire_due()` 와 **같은 기준**인 `owners` 를 쓴다.
            owed = self._rollback_debt(policy_id)
            if owed is None:
                # 확실히 쓰지 않았다.  `record_applied` 도 부르지 않는다 -- 그 호출은
                # `_controls` 에 `writeMayHaveOccurred=False` 이력을 남기는데, 정책이
                # 된 적 없는 요청에 그 이력만 남는다.  그리고 `RuntimeError` 로 새면
                # HTTP 500 이 되어 adapter 가 **ACK 유실**로 읽고 불필요한 복구를 연다
                # (500 → R1Error → UNKNOWN).  쓰지 않았다는 사실을 client 까지 보낸다.
                from .producer import A1Conflict
                raise A1Conflict(
                    f"nothing was applied; the write was refused before it began: {exc}"
                ) from exc
            # 원장이 이 scope 를 아직 들고 있다 = 워커가 롤백을 빚고 있다.  되감으면
            # 그 빚이 사라지므로 NACK 만 적고 정책은 남긴다.  **그리고 "쓰지 않았다" 고
            # 적으면 안 된다** -- 빚이 있다는 것은 라디오가 값을 받았을 수 있다는 뜻이고,
            # adapter 는 그 칸을 믿고 정책 되읽기를 건너뛴다.
            self._producer.record_applied(
                policy_type_id, policy_id, control_ack=False, observed_config=None,
                write_may_have_occurred=True, restore_config=owed,
            )
            return
        # 예외가 아닌 **정상 NACK** 도 같은 함정을 갖고 있었다: `_send_apply` 는 라디오를
        # 때리기 전에 `WRITE_STARTED` 를 굳히므로, 거기서 돌아온 NACK 은 "쓰지 않았다" 가
        # 아니라 "ACK 을 못 받았다" 이다.  검증된 롤백으로 원상복구된 경우에만 남은 쓰기가
        # 없다 -- 그때는 원장이 이미 소유권을 놓았으므로 `_rollback_debt` 이 None 을 준다.
        # (형제 자리를 같이 고친다: 예외 경로만 고치면 더 흔한 이 경로가 그대로 거짓말한다.)
        #
        # 2026-09-23 audit: 위 주장은 틀렸다 -- `_restore` 는 검증된 롤백 뒤에도 `owners`
        # 를 지우지 않는다(지우면 `expire_due` 가 이 정책을 못 봐 프로듀서의 scope 주장이
        # 영구히 남는다).  그래서 NACK + 검증된 롤백이 `writeMayHaveOccurred: true` 로
        # 기록돼 UNKNOWN→PARTIAL_APPLY 가 됐다.  라디오가 기준선으로 **되읽혔다**는 결과
        # 자체가 빚 없음의 증거다.
        debt = (None if outcome.control_ack or outcome.rollback_verified
                else self._rollback_debt(policy_id))
        self._producer.record_applied(
            policy_type_id,
            policy_id,
            control_ack=outcome.control_ack,
            observed_config=outcome.observed_config if outcome.effect_verified else None,
            write_may_have_occurred=debt is not None,
            restore_config=debt,
        )

    def _rollback_debt(self, policy_id: str) -> Optional[Mapping[str, Any]]:
        """빚이 있으면 **되돌릴 기준선**을, 없으면 ``None``.

        판별자는 `expire_due()` 와 같다(`owners[scopeKey] == policyId`).  `owners` 는
        `apply` 가 라디오를 때리기 **전에** 심어 `flush` 로 굳고, 검증된 롤백·withdraw
        에서만 지워진다.  그래서 "이 요청이 썼을 수 있나" 의 정확한 판별자다.
        값까지 함께 돌려주는 이유는, 롤백을 요청했다고 적으려면 **무엇으로 되돌릴지**를
        같이 말해야 하기 때문이다(A1 status 스키마).
        """
        try:
            state = self._ledger.snapshot()
            entry = (state.get("entries") or {}).get(policy_id)
            if not isinstance(entry, Mapping):
                return None
            if (state.get("owners") or {}).get(entry.get("scopeKey")) != policy_id:
                return None
            baseline = entry.get("baseline")
            return dict(baseline) if isinstance(baseline, Mapping) else {}
        except Exception:          # noqa: BLE001 - 판정 불가면 빚이 있다고 본다
            return {}

    def _delete_from_a1(self, policy_id: str) -> None:
        assert self._producer is not None
        record = self._producer.policy_record(policy_id)
        try:
            self.withdraw(record["policyTypeId"], policy_id, record["policy"])
        except IdentityRefusal as exc:
            if not self._identity_superseded(policy_id):
                from .producer import A1Conflict
                raise A1Conflict(f"DELETE rollback gate failed: {exc}") from exc
            self._retire_unrestorable(policy_id, 1, 0.0)
            return
        except LiveWorkerError as exc:
            # Imported lazily: producer owns its transport error vocabulary.
            from .producer import A1Conflict
            raise A1Conflict(f"DELETE rollback gate failed: {exc}") from exc
        self._producer.delete_after_rollback(
            record["policyTypeId"], policy_id, self._delete_capability
        )

    # -- public lifecycle -------------------------------------------------

    @serialized
    def apply(self, policy_type_id: str, policy_id: str,
              policy: Mapping[str, Any]) -> ControlOutcome:
        action = self._action(policy_type_id)
        desired, cell_id, requested_ue = self._policy_values(action, policy)
        self._check_validity(policy)
        identity = self._resolve_scope(action, requested_ue, cell_id)
        scope_key = self._scope_key(action, identity, cell_id)
        digest = jcs_sha256(policy)

        with self._ledger.edit() as state:
            entries, owners = state["entries"], state["owners"]
            owner = owners.get(scope_key)
            if owner is not None and owner != policy_id:
                raise ScopeConflict(f"{scope_key} is already owned by policy {owner}")
            entry = entries.get(policy_id)
            if entry is not None and owners.get(entry.get("scopeKey")) != policy_id:
                # 이 id 의 생애는 이미 끝났다(검증된 롤백이나 DELETE 가 owners 에서
                # 지웠고 이력만 남았다).  그 위에 새 적용을 얹으면 두 가지가 조용히
                # 깨진다 -- 같은 digest 면 `_reconcile_existing` 이 옛 결과를 돌려주며
                # 소유권을 다시 심지 않아 `expire_due()` 가 영영 못 보고, 다른 digest 면
                # **옛 기준선**으로 복구한다(2026-09-21 codex 재현: 외부가 18 로 바뀐 뒤에도
                # 12 로 되돌렸다).  한 생애의 attempts·baseline 은 그 생애의 것이다.
                raise ScopeConflict(
                    f"policy id {policy_id} has completed its lifecycle; reuse is refused"
                )
            if entry is not None and entry.get("scopeKey") != scope_key:
                raise ScopeConflict("a policy id cannot move to another live writer slot")
            if (entry is not None
                    and self._entry_identity(action, entry) != identity):
                raise IdentityRefusal(
                    "fresh scope identity/epoch differs from the persisted policy binding"
                )
            attempts = entry.setdefault("attempts", {}) if entry is not None else {}
            existing = attempts.get(digest)
            if existing is not None:
                return self._reconcile_existing(
                    action, entry, existing, identity, state
                )

            baseline = self._read_values(action, identity, min_line=0)
            # 여기서 `{"maxDlPrbs": 0}` 을 지어내 왔다.  **0 은 "할당 0" 이 아니라 "무제한"**
            # 이므로, 실제 cap 이 12 였던 UE 를 복구할 때 12 가 아니라 **캡 해제**로 되돌린다
            # -- 조용히 실험 조건을 바꾸는 복구다.  기준선을 못 읽었으면 그것은 관측 실패이지
            # 기본값을 아는 상황이 아니므로, 다른 축과 같이 **쓰기를 거절**한다.
            # (프로듀서의 scope 주장은 이 거절과 함께 풀린다 -- `_apply_from_a1` 참조.
            #  그 되감기가 없던 동안에는 거절이 늘수록 축이 고착됐다.)
            if baseline is None:
                raise LiveWorkerRefusal(
                    f"fresh {action.counter} baseline is unavailable; refusing write"
                )
            if action.action_id == 6:
                baseline["_scope"] = copy.deepcopy(desired["_scope"])
            if entry is None:
                entry = {
                    "policyTypeId": policy_type_id,
                    "scopeKey": scope_key,
                    "baseline": baseline,
                    "identity": self._identity_json(identity),
                    "attempts": {},
                    "notAfter": policy["validity"]["notAfter"],
                }
                entries[policy_id] = entry
                attempts = entry["attempts"]
            else:
                entry["notAfter"] = policy["validity"]["notAfter"]
            owners[scope_key] = policy_id
            attempt = {
                "phase": "PREPARED",
                "desired": desired,
                "policy": copy.deepcopy(dict(policy)),
                "controlAck": False,
                "rollbackAttempted": False,
                "rollbackVerified": False,
            }
            attempts[digest] = attempt
            entry["currentDigest"] = digest
            return self._send_apply(action, entry, attempt, identity, state)

    @serialized
    def withdraw(self, policy_type_id: str, policy_id: str,
                 policy: Optional[Mapping[str, Any]] = None) -> ControlOutcome:
        action = self._action(policy_type_id)
        del policy
        with self._ledger.edit() as state:
            entry = state["entries"].get(policy_id)
            if entry is None:
                raise LiveWorkerError(f"policy {policy_id} has no persisted baseline")
            identity = self._entry_identity(action, entry)
            # UE actions remain bound to one exact UE identity.  Cell/slice
            # actions are bound only to the node epoch; their encoder may use
            # any freshly validated UE header on that cell at send time.
            requested_ue = (
                str(identity.amf_ue_ngap_id)
                if isinstance(identity, _Identity) else None
            )
            fresh = self._resolve_scope(action, requested_ue, self._cell_id)
            if fresh != identity:
                moved, ours = self._cell_epoch_moved(entry)
                if moved != fresh or not ours:
                    raise IdentityRefusal("scope identity/epoch changed before rollback")
                # Same cell, new E2 connection epoch, and our value is still on the
                # radio -- the link reconnected without the gNB restarting, so the
                # baseline is still the one to put back.  Restore under the new epoch.
                entry["identity"] = self._identity_json(fresh)
            result = self._restore(action, entry, fresh, state)
            if not result.rollback_verified:
                raise LiveWorkerError(
                    "persisted baseline was not exactly restored; policy retained"
                )
            state["owners"].pop(entry["scopeKey"], None)
            return result

    #: How long an unrestorable rollback must stay unrestorable before it is
    #: retired.  The gap a keeper restart leaves is 10 s at the median and
    #: 21 s at p99 (84 measured gaps, 2026-09-18), so half an hour cannot be
    #: one: a momentary gap must never erase a UE we could still have used.
    _RETIRE_AFTER_S = 1800.0

    def _rollback_target_is_gone(self, exc: BaseException, stuck_s: float,
                                 policy_id: str) -> bool:
        """Is there provably nothing left to restore?

        Only a **UE-scoped** policy can be retired this way: its knob lived in a
        UE context that re-registration tears down.  A cell-scoped setting
        (attenuation, MCS bounds) stays on the radio whatever the UEs do, so
        retiring it would drop the only owner of a value still in force
        (2026-09-23 audit).  Beyond that, three things must hold together,
        because each alone lies:

        * the failure is the identity gate, not a transport or producer fault
          -- otherwise an outage would retire policies that still hold the
          radio, and we would forget a cap we had left on;
        * the bound cell is producing **fresh** KPM that names at least one UE
          -- positive evidence that the UE list is readable at all, rather
          than an empty or stale read we would misread as departure;
        * it has been that way for ``_RETIRE_AFTER_S``.
        """
        cause = exc.__cause__ if exc.__cause__ is not None else exc
        if not isinstance(cause, IdentityRefusal):
            return False
        if stuck_s < self._RETIRE_AFTER_S:
            return False
        with self._ledger.edit(read_only=True) as state:
            entry = state["entries"].get(policy_id)
            scope_kind = (entry.get("identity") or {}).get("scopeKind") if entry else None
        if scope_kind != "UE":
            return False
        return any(record.get("ues")
                   for _, record in self._gate.records()
                   if self._gate.fresh(record))

    def _identity_superseded(self, policy_id: str) -> bool:
        """Has the UE this policy was bound to provably re-registered?

        2026-09-23 (v46r8 board 462): a steered UE died mid-trial; the
        hand-back and then the DELETE were refused because the policy names
        the old AMF UE NGAP ID, and the policy would have waited
        ``_RETIRE_AFTER_S`` (30 min) at the RIC while the trial locked down.
        Re-registration mints a new id and a re-attached UE starts at the
        baseline (measured 2026-09-18, see ``expire_due``), so once the role's
        control header names a **different** id, the old context is gone and
        there is nothing to restore.  All three must hold:

        * the persisted binding is a UE binding with a role tag;
        * that role's header on disk names another AMF UE NGAP ID;
        * this cell's fresh KPM names UEs (the list is readable) and none of
          them is the old id.
        """
        with self._ledger.edit() as state:
            entry = copy.deepcopy(state["entries"].get(policy_id) or {})
            identity = dict(entry.get("identity") or {})
        if identity.get("scopeKind") == "CELL":
            # A cell scope is superseded when its node epoch has advanced and the
            # value we set is no longer on the radio: a restarted gNB comes back
            # at its conf value, so there is nothing of ours left to restore.
            return self._cell_epoch_moved(entry)[1] is False
        if identity.get("scopeKind") != "UE" or not identity.get("ueTag"):
            return False
        old = _as_int(identity.get("amfUeNgapId"))
        old_ran = _as_int(identity.get("ranUeId"))
        current = dict(self._headers_on_disk()).get(identity["ueTag"])
        if current is None:
            return False
        # 2026-09-27 11:55: a core reset restarts AMF numbering, and ue2's new context
        # came back as AMF UE NGAP ID 2 again -- the same id, a different RRC UE id
        # (2 -> 1).  Comparing the AMF id alone left policy 2df5dcc3 owning ue2's
        # priority scope for good ("scope identity/epoch changed before rollback" every
        # 300 s) and the next board's ue2 priority trials were 409s.  The context is
        # the (AMF id, RRC UE id) pair.
        same_amf = current["RC_HEADER_AMF_UE_NGAP_ID"] == old
        if same_amf and (old_ran is None or current["RC_HEADER_RRC_UE_ID"] == old_ran):
            return False
        fresh = [r for _, r in self._gate.records() if self._gate.fresh(r) and r.get("ues")]
        if not fresh:
            return False
        # The old context is still live if fresh KPM shows its (AMF, RRC) pair; a row
        # without an RRC id cannot rule it out (Codex review).
        return not any(_as_int(ue.get("amf_ue_ngap_id")) == old
                       and (old_ran is None or _as_int(ue.get("ran_ue_id")) in (None, old_ran))
                       for record in fresh for ue in record["ues"]
                       if isinstance(ue, Mapping))

    def _cell_epoch_moved(self, entry: Mapping[str, Any]
                          ) -> tuple[Optional[_CellIdentity], Optional[bool]]:
        """For a cell-scoped entry whose node epoch has since changed: the fresh
        identity and whether our value is still in force (``None`` = unreadable).

        2026-09-24: the cell scope key has no epoch in it (``action=X/target=cell``),
        and ``withdraw`` refused any epoch change while both retirement paths were
        UE-only -- so one gNB restart left a cell policy owning its scope for good
        and every later cell write of that action was a ``ScopeConflict``.  The
        epoch alone does not say what happened: a restarted gNB is back at its conf
        value (nothing to restore), while an E2 reconnect without a restart leaves
        our value in force (restore it).  The fresh counter tells them apart.
        """
        identity = entry.get("identity") or {}
        if identity.get("scopeKind") != "CELL":
            return None, None
        try:
            action = self._action(entry["policyTypeId"])
            old = self._identity_from_json(identity)
            fresh = self._resolve_cell(old.cell_id)
        except (KeyError, ValueError, TypeError, LiveWorkerError):
            return None, None
        if fresh.epoch == old.epoch or fresh.nb_id != old.nb_id:
            return None, None
        latest = (entry.get("attempts") or {}).get(entry.get("currentDigest")) or {}
        observed = self._read_values(action, fresh, min_line=0)
        if observed is None or not latest.get("desired"):
            return fresh, None
        return fresh, self._same_values(observed, latest["desired"])

    def _retire_unrestorable(self, policy_id: str, attempts: int,
                             stuck_s: float) -> None:
        """Release the scope and drop the record, saying what really happened."""
        assert self._producer is not None
        scope_key = ""
        with self._ledger.edit() as state:
            entry = state["entries"].get(policy_id)
            if entry is not None:
                scope_key = entry["scopeKey"]
                if state["owners"].get(scope_key) == policy_id:
                    state["owners"].pop(scope_key, None)
        try:
            record = self._producer.policy_record(policy_id)
            self._producer.delete_after_rollback(
                record["policyTypeId"], policy_id, self._delete_capability)
        except Exception as exc:  # noqa: BLE001 - retirement must not wedge the timer
            _LOG.warning(
                "Campaign-5 retired policy_id=%s but could not drop the producer "
                "record: %s", policy_id, exc)
        _LOG.warning(
            "Campaign-5 expiry rollback retired, NOT performed: policy_id=%s "
            "scope=%s (attempt %d, stuck for %.0f min).  The UE context that "
            "held this setting is gone and a re-attached UE starts at the "
            "baseline, so no control was sent and none was needed.",
            policy_id, scope_key, attempts, stuck_s / 60.0,
        )

    @serialized
    def expire_due(self) -> tuple[str, ...]:
        """Restore and delete expired policies; intended for the server timer."""
        if self._producer is None:
            return ()
        expired: list[str] = []
        # Snapshot without retaining the ledger lock across producer callbacks.
        with self._ledger.edit() as state:
            candidates = [
                pid for pid, entry in state["entries"].items()
                if self._parse_time(entry["notAfter"]) <= self._clock()
                and state["owners"].get(entry["scopeKey"]) == pid
            ]
        retry_at = self.__dict__.setdefault("_expiry_retry_at", {})
        failures = self.__dict__.setdefault("_expiry_failures", {})
        for policy_id in candidates:
            now = self._clock().timestamp()
            if retry_at.get(policy_id, 0.0) > now:
                continue
            try:
                record = self._producer.policy_record(policy_id)
                self._delete_from_a1(policy_id)
            except Exception as exc:
                # Each transaction gets its own clock reading: one restore can
                # take 36 s, and a back-off counted from the start of the pass
                # was already over by the time it was set (2026-09-23 audit).
                now = self._clock().timestamp()
                # Retain the policy/owner so a later pass retries, but never make
                # a failed safety restore invisible to operations.  Back off: a
                # restore that cannot resolve its UE (the UE left) was retried
                # every second for hours, reading the whole KPM stream each time
                # (2026-09-15, producer at 100 % CPU).  Capped at 300 s.
                failures[policy_id] = failures.get(policy_id, 0) + 1
                retry_at[policy_id] = now + min(300.0, 2.0 ** failures[policy_id])
                attempts = failures[policy_id]
                first = self.__dict__.setdefault("_expiry_first_failure", {})
                first.setdefault(policy_id, now)
                stuck_s = now - first[policy_id]
                # The traceback is worth reading once.  Repeating it every 300 s
                # forever is what buried this: on 2026-09-17 six policies had been
                # retrying for up to **119 attempts (about ten hours)** and the
                # producer log was a megabyte of the same stack, so nobody saw
                # that a policy was permanently stuck.  Print the stack for the
                # first few, then one line that says how long and how often --
                # a stuck policy has to be *legible*, not merely recorded.
                if (self._rollback_target_is_gone(exc, stuck_s, policy_id)
                        or self._identity_superseded(policy_id)):
                    # The knob lived in a UE context the RAN has since torn
                    # down, and a re-attached UE comes up at the baseline --
                    # measured 2026-09-18, while 33 policies had been
                    # "retrying" their restore for up to 16 hours:
                    # RAN.UE.DlPrbCap read 0 and RAN.UE.PfWeight 1.0 on every
                    # re-attached UE.  There is nothing left to restore, and
                    # the scope key (action + amfUeNgapId) can never be asked
                    # for again because re-registration mints a new one.  Say
                    # what actually happened -- the baseline came back with
                    # the context, not from a control we sent -- and stop.
                    self._retire_unrestorable(policy_id, attempts, stuck_s)
                    failures.pop(policy_id, None)
                    retry_at.pop(policy_id, None)
                    continue
                if attempts <= 3:
                    _LOG.exception(
                        "Campaign-5 expiry rollback failed; policy retained: "
                        "policy_id=%s (attempt %d, next retry in %.0f s)",
                        policy_id, attempts, retry_at[policy_id] - now,
                    )
                else:
                    _LOG.warning(
                        "Campaign-5 expiry rollback still failing; policy retained: "
                        "policy_id=%s (attempt %d, stuck for %.0f min, next retry "
                        "in %.0f s): %s",
                        policy_id, attempts, stuck_s / 60.0,
                        retry_at[policy_id] - now,
                        f"{type(exc).__name__}: {exc}",
                    )
                continue
            failures.pop(policy_id, None)
            retry_at.pop(policy_id, None)
            if record:
                expired.append(policy_id)
        return tuple(expired)

    # -- apply/recovery ---------------------------------------------------

    def _send_apply(self, action: _Action, entry: dict[str, Any],
                    attempt: dict[str, Any], identity: _ScopeIdentity,
                    state: dict[str, Any]) -> ControlOutcome:
        # Header resolution is a pre-write guard.  Do not cross the durable
        # ambiguity boundary until an exact control identity is available.
        control_identity = self._resolve_control_identity(identity)
        marker = self._gate.line_count()
        sent_after_us = int(self._clock().timestamp() * 1_000_000)
        attempt["phase"] = "WRITE_STARTED"  # durable before the ambiguous call
        self._ledger.flush(state)
        ack, detail = self._spaced_control(action, control_identity, identity, attempt["desired"])
        attempt["controlAck"] = ack
        attempt["phase"] = "ACKED" if ack else "CONTROL_UNKNOWN"
        self._ledger.flush(state)
        if ack:
            observed = self._wait_for_values(
                action, identity, marker, attempt["desired"], sent_after_us
            )
            if observed is not None:
                attempt["phase"] = "APPLIED_VERIFIED"
                return ControlOutcome(True, True, self._observed_config(
                    action, attempt["policy"], observed
                ), False, False, detail)
        # Once WRITE_STARTED is durable, every non-verified outcome is treated
        # as possibly written and is restored.  A timeout is not a clean NACK.
        restored = self._restore(action, entry, identity, state)
        return ControlOutcome(
            ack, False, None, True, restored.rollback_verified,
            detail + "; " + restored.detail,
        )

    def _reconcile_existing(self, action: _Action, entry: dict[str, Any],
                            attempt: dict[str, Any], identity: _ScopeIdentity,
                            state: dict[str, Any]) -> ControlOutcome:
        phase = attempt.get("phase")
        if phase == "APPLIED_VERIFIED":
            observed = self._read_values(action, identity, min_line=0)
            if observed is not None and self._same_values(observed, attempt["desired"]):
                return ControlOutcome(True, True, self._observed_config(
                    action, attempt["policy"], observed
                ), False, False, "durable duplicate reverified; no control re-sent")
            restored = self._restore(action, entry, identity, state)
            return ControlOutcome(
                True, False, None, True, restored.rollback_verified,
                "durable effect drifted; converged by baseline restore",
            )
        if phase == "ROLLED_BACK_VERIFIED":
            return ControlOutcome(
                bool(attempt.get("controlAck")), False, None, True, True,
                "durable duplicate already restored; no control re-sent",
            )
        # Crash after WRITE_STARTED is ambiguous.  Converge by observation,
        # never by re-sending the requested write.
        observed = self._read_values(action, identity, min_line=0)
        if observed is not None and self._same_values(observed, attempt["desired"]):
            attempt["phase"] = "APPLIED_VERIFIED"
            return ControlOutcome(
                bool(attempt.get("controlAck", True)), True,
                self._observed_config(action, attempt["policy"], observed),
                False, False, "restart reconciled requested value from KPM",
            )
        restored = self._restore(action, entry, identity, state)
        return ControlOutcome(
            bool(attempt.get("controlAck")), False, None, True,
            restored.rollback_verified, "restart converged by baseline restore",
        )

    def _restore(self, action: _Action, entry: dict[str, Any],
                 identity: _ScopeIdentity, state: dict[str, Any]) -> ControlOutcome:
        attempts = entry.get("attempts", {})
        latest = attempts.get(entry.get("currentDigest"))
        if latest is None and attempts:  # tolerate the initial ledger format
            latest = list(attempts.values())[-1]
        if latest is not None and latest.get("phase") == "ROLLED_BACK_VERIFIED":
            observed = self._read_values(action, identity, min_line=0)
            if observed is not None and self._same_values(observed, entry["baseline"]):
                return ControlOutcome(
                    False, False, None, True, True,
                    "persisted baseline remains freshly verified",
                )
            raise LiveWorkerError("previously restored baseline no longer matches")
        if latest is not None and latest.get("phase") in {
            "ROLLBACK_STARTED", "RECOVERY_PENDING"
        }:
            observed = self._read_values(action, identity, min_line=0)
            if observed is not None and self._same_values(observed, entry["baseline"]):
                latest["phase"] = "ROLLED_BACK_VERIFIED"
                latest["rollbackVerified"] = True
                return ControlOutcome(
                    False, False, None, True, True,
                    "restart observed the previously sent baseline rollback",
                )
            raise LiveWorkerError(
                "rollback delivery is ambiguous; refusing a duplicate control"
            )
        # In particular, a cell with no attached UE cannot currently encode a
        # Format-1 control header.  Resolve before ROLLBACK_STARTED so that this
        # pre-send refusal leaves the verified policy retryable when any UE
        # later attaches; it is not ambiguous delivery.
        control_identity = self._resolve_control_identity(identity)
        marker = self._gate.line_count()
        sent_after_us = int(self._clock().timestamp() * 1_000_000)
        if latest is not None:
            latest["rollbackAttempted"] = True
            latest["phase"] = "ROLLBACK_STARTED"
        self._ledger.flush(state)
        ack, detail = self._spaced_control(action, control_identity, identity, entry["baseline"])
        if latest is not None:
            latest["rollbackAck"] = ack
        self._ledger.flush(state)
        observed = (
            self._wait_for_values(
                action, identity, marker, entry["baseline"], sent_after_us
            )
            if ack else None
        )
        verified = observed is not None
        if latest is not None:
            latest["rollbackVerified"] = verified
            latest["phase"] = "ROLLED_BACK_VERIFIED" if verified else "RECOVERY_PENDING"
        self._ledger.flush(state)
        if not verified:
            return ControlOutcome(
                ack, False, None, True, False,
                "baseline rollback was not ACKed and exactly read back",
            )
        return ControlOutcome(ack, False, None, True, True,
                              "persisted baseline restored and verified: " + detail)

    # -- identity and KPM -------------------------------------------------

    def _headers_on_disk(self) -> list[tuple[str, dict[str, int]]]:
        result: list[tuple[str, dict[str, int]]] = []
        for path in sorted(self._headers.glob("*-hdr.env")):
            values: dict[str, int] = {}
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, raw = line.split("=", 1)
                if key not in _HEADER_KEYS:
                    continue
                value = _as_int(raw.strip().strip("'\""))
                if value is not None:
                    values[key] = value
            if set(values) == set(_HEADER_KEYS):
                result.append((path.name[:-len("-hdr.env")], values))
        return result

    def _resolve_identity(self, requested_ue: Optional[str], cell_id: str) -> _Identity:
        if str(cell_id) != self._cell_id:
            raise IdentityRefusal(
                f"policy cell {cell_id!r} differs from bound cell {self._cell_id!r}"
            )
        candidates: list[_Identity] = []
        records = self._gate.records()
        for ue_tag, header in self._headers_on_disk():
            amf_id = header["RC_HEADER_AMF_UE_NGAP_ID"]
            if requested_ue is not None and str(requested_ue) not in {ue_tag, str(amf_id)}:
                continue
            for _, record in reversed(records):
                if not self._gate.fresh(record):
                    continue
                epoch = _as_int(record.get("connection_epoch"))
                if epoch is None:
                    continue
                for ue in record.get("ues", ()):
                    if not isinstance(ue, Mapping):
                        continue
                    guami = ue.get("guami")
                    if (_as_int(ue.get("amf_ue_ngap_id")) != amf_id
                            or _as_int(ue.get("ran_ue_id")) != header["RC_HEADER_RRC_UE_ID"]
                            or not isinstance(guami, Mapping)):
                        continue
                    normalized = {key: _as_int(guami.get(key)) for key in _GUAMI_KEYS}
                    if any(value is None for value in normalized.values()):
                        continue
                    if any(normalized[key] != header[header_key]
                           for key, header_key in _GUAMI_KEYS.items()):
                        continue
                    candidates.append(_Identity(
                        ue_tag, amf_id, header["RC_HEADER_RRC_UE_ID"],
                        {key: int(value) for key, value in normalized.items()}, epoch,
                    ))
                    break
                if candidates and candidates[-1].ue_tag == ue_tag:
                    break
        unique = {(c.ue_tag, c.amf_ue_ngap_id, c.ran_ue_id, c.epoch): c for c in candidates}
        if requested_ue is not None and len(unique) != 1:
            raise IdentityRefusal("UE scope has no unique fresh KPM/header identity")
        if requested_ue is None:
            # Cell/slice controls still require Control Header Format 1.  Use a
            # deterministic, freshly attributed UE on the bound cell.
            ordered = sorted(unique.values(), key=lambda value: value.ue_tag)
            if not ordered:
                raise IdentityRefusal("bound cell has no fresh UE control header")
            return ordered[0]
        return next(iter(unique.values()))

    def _resolve_cell(self, cell_id: str) -> _CellIdentity:
        if str(cell_id) != self._cell_id:
            raise IdentityRefusal(
                f"policy cell {cell_id!r} differs from bound cell {self._cell_id!r}"
            )
        epochs = {
            epoch
            for _, record in self._gate.records()
            if self._gate.fresh(record)
            for epoch in (_as_int(record.get("connection_epoch")),)
            if epoch is not None
        }
        if len(epochs) != 1:
            raise IdentityRefusal("cell scope has no unique fresh node epoch")
        return _CellIdentity(self._cell_id, self._nb_id, next(iter(epochs)))

    def _resolve_scope(self, action: _Action, requested_ue: Optional[str],
                       cell_id: str) -> _ScopeIdentity:
        if action.ue_scoped:
            return self._resolve_identity(requested_ue, cell_id)
        return self._resolve_cell(cell_id)

    def _entry_identity(self, action: _Action,
                        entry: dict[str, Any]) -> _ScopeIdentity:
        identity = self._identity_from_json(entry["identity"])
        if not action.ue_scoped and isinstance(identity, _Identity):
            # Migrate the first live-worker ledger format.  It persisted an
            # arbitrary UE even for cell actions; only its node epoch is part
            # of the actual policy scope.
            identity = _CellIdentity(self._cell_id, self._nb_id, identity.epoch)
            entry["identity"] = self._identity_json(identity)
        return identity

    def _read_values(self, action: _Action, identity: _ScopeIdentity,
                     *, min_line: int,
                     min_received_us: Optional[int] = None) -> Optional[dict[str, Any]]:
        for _, record in reversed(self._gate.records(min_line=min_line)):
            if not self._gate.fresh(record) or _as_int(record.get("connection_epoch")) != identity.epoch:
                continue
            received_us = _as_int(record.get("recv_unix_us"))
            if (min_received_us is not None
                    and (received_us is None or received_us < min_received_us)):
                continue
            measurements: list[Mapping[str, Any]] = []
            if action.ue_scoped:
                if not isinstance(identity, _Identity):
                    raise LiveWorkerError("UE action has a non-UE persisted identity")
                matching_ue: Optional[Mapping[str, Any]] = None
                for ue in record.get("ues", ()):
                    if isinstance(ue, Mapping) and self._same_ue(ue, identity):
                        matching_ue = ue
                        break
                if matching_ue is None:
                    continue
                measurements.extend(m for m in matching_ue.get("measurements", ())
                                    if isinstance(m, Mapping))
            else:
                measurements.extend(m for m in record.get("measurements", ())
                                    if isinstance(m, Mapping))
            values = self._extract_values(action, measurements)
            if values is not None:
                return values
        return None

    def _wait_for_values(self, action: _Action, identity: _ScopeIdentity,
                         min_line: int, expected: Mapping[str, Any],
                         min_received_us: int) -> Optional[dict[str, Any]]:
        deadline = time.monotonic() + self._deadline_s
        while True:
            observed = self._read_values(
                action, identity, min_line=min_line,
                min_received_us=min_received_us,
            )
            if observed is not None and self._same_values(observed, expected):
                return observed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._sleep(min(self._poll_s, remaining))

    @staticmethod
    def _same_ue(ue: Mapping[str, Any], identity: _Identity) -> bool:
        if (_as_int(ue.get("amf_ue_ngap_id")) != identity.amf_ue_ngap_id
                or _as_int(ue.get("ran_ue_id")) != identity.ran_ue_id):
            return False
        guami = ue.get("guami")
        return isinstance(guami, Mapping) and all(
            _as_int(guami.get(key)) == value for key, value in identity.guami.items()
        )

    @staticmethod
    def _extract_values(action: _Action,
                        measurements: Sequence[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
        values: dict[str, Any] = {}
        aliases = {
            "min": "minDlMcs", "minDlMcs": "minDlMcs", "distBin0": "minDlMcs",
            "max": "maxDlMcs", "maxDlMcs": "maxDlMcs", "distBin1": "maxDlMcs",
            "minPrbPolicyRatio": "minPrbPolicyRatio",
            "maxPrbPolicyRatio": "maxPrbPolicyRatio",
            "dedicatedPrbPolicyRatio": "dedicatedPrbPolicyRatio",
        }
        unlabeled: list[Any] = []
        for metric in measurements:
            name = metric.get("name")
            if not isinstance(name, str):
                continue
            if name == action.counter:
                raw = metric.get("value")
                if isinstance(raw, Mapping):
                    for field in action.value_fields:
                        if field in raw:
                            values[field] = raw[field]
                    continue
                label = metric.get("field") or metric.get("label") or metric.get("component")
                field = aliases.get(str(label)) if label is not None else None
                if field in action.value_fields:
                    values[field] = raw
                else:
                    unlabeled.append(raw)
                continue
            for separator in (".", "/", "_"):
                prefix = action.counter + separator
                if name.startswith(prefix):
                    field = aliases.get(name[len(prefix):])
                    if field in action.value_fields:
                        values[field] = metric.get("value")
                    break
        if len(action.value_fields) == 1 and len(unlabeled) == 1:
            values[action.value_fields[0]] = unlabeled[0]
        # Slice's deployed effect counter proves only the minimum ratio.  That
        # is insufficient to persist/restore the three-leaf quota, so remain
        # fail-closed unless labeled max and dedicated counters are also present.
        if set(values) != set(action.value_fields):
            return None
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               for value in values.values()):
            return None
        return values

    # -- control rendering ------------------------------------------------

    def _resolve_control_identity(self, identity: _ScopeIdentity) -> _Identity:
        if isinstance(identity, _Identity):
            return identity
        # A cell control needs a UE on the cell for its Format-1 header.  In a joint trial the
        # hand-back that brings the UE home is ACKed before the UE has re-attached, and the
        # power restore that follows found the cell empty and locked the board down (Codex
        # review of 507e23efc, 2026-09-26).  Wait a bounded time for a UE to appear.
        waited = 0.0
        while True:
            try:
                control_identity = self._resolve_identity(None, identity.cell_id)
                break
            except IdentityRefusal as exc:
                if "no fresh UE control header" not in str(exc) or waited >= self._cell_ue_wait_s:
                    raise
                self._sleep(1.0)
                waited += 1.0
        if control_identity.epoch != identity.epoch:
            raise IdentityRefusal(
                "cell control header differs from the captured node epoch"
            )
        return control_identity

    def _spaced_control(self, action: _Action, control_identity: _Identity,
                        identity: _ScopeIdentity, values: Mapping[str, Any]) -> tuple[bool, str]:
        """Action 104 (cell power) keeps the gNB's 15 s write interval and skips a write the
        cell already holds (v5, 2026-09-26: board 670/block 11 writes refused 4 s apart).
        Every other action is sent as before."""
        if action.action_id != 104:
            return self._run_control(action, control_identity, values)
        # Skip only on a reading that cannot predate the last write (Codex review 2026-09-26):
        # within 10 s of an RF write the KPM value may still be the old one, and skipping a
        # rollback on it would leave the change in place.
        since = seconds_since_power_write()
        current = self._read_values(action, identity, min_line=0)
        if (current is not None and self._same_values(current, values)
                and (since is None or since > POWER_SKIP_AFTER_S)):
            return True, "cell already holds the requested attenuation; no RF write sent"
        wait = power_write_wait_s()
        if wait > 0:
            # Known limit: a policy whose validity ends inside this wait is still written once;
            # the producer's expiry restore then returns the cell to its baseline.
            self._sleep(wait)
        try:
            return self._run_control(action, control_identity, values)
        finally:
            mark_power_write()

    def _run_control(self, action: _Action, identity: _Identity,
                     values: Mapping[str, Any]) -> tuple[bool, str]:
        env = os.environ.copy()
        header_env = self._control_header_env(identity)
        env.update(header_env)
        env["RC_ACTION"] = str(action.action_id)
        for field, env_name in action.env_fields.items():
            env[env_name] = str(values[field])
        if action.action_id == 6:
            # Slice identity is immutable policy scope, not operator free text.
            policy = values.get("_scope")
            if not isinstance(policy, Mapping):
                raise LiveWorkerRefusal("slice action lacks its typed PLMN/S-NSSAI scope")
            plmn, snssai = policy["plmnId"], policy["snssai"]
            env.update({
                "RC_SLICE_MCC": str(plmn["mcc"]),
                "RC_SLICE_MNC": str(plmn["mnc"]),
                "RC_SLICE_MNC_LEN": str(len(str(plmn["mnc"]))),
                "RC_SLICE_SST": str(snssai["sst"]),
                "RC_SLICE_HAS_SD": "1" if "sd" in snssai else "0",
                "RC_SLICE_SD": str(int(snssai.get("sd", "0"), 16)),
            })
        try:
            with self._invocation_header(header_env) as header_path:
                result = self._runner(
                    [str(self._fire), "--header-file", str(header_path)],
                    env=env,
                    timeout=self._deadline_s,
                    capture_output=True,
                    text=True,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            text = "\n".join(filter(None, [str(exc.stdout or ""), str(exc.stderr or "")]))
            return False, "our_rc_xapp deadline expired: " + text[-200:]
        except OSError as exc:
            return False, f"our_rc_xapp launch failed: {exc}"
        output = "\n".join((str(getattr(result, "stdout", "") or ""),
                            str(getattr(result, "stderr", "") or "")))
        action_seen = re.search(rf"\baction\s*=\s*{action.action_id}\b", output, re.I)
        success = re.search(r"\bsuccess\s*=\s*(?:1|true|yes)\b", output, re.I)
        ack = re.search(r"\bACK\b|Control\s+Acknowledge", output, re.I)
        refused = re.search(r"\bNACK\b|reject|failure|capability.*(?:fail|missing)", output, re.I)
        ok = (getattr(result, "returncode", 1) == 0 and action_seen is not None
              and success is not None and ack is not None and refused is None)
        return ok, output[-400:] if output else "our_rc_xapp produced no outcome lines"

    def _control_header_env(self, identity: _Identity) -> dict[str, str]:
        values = {
            "RC_HEADER_RRC_UE_ID": identity.ran_ue_id,
            "RC_HEADER_AMF_UE_NGAP_ID": identity.amf_ue_ngap_id,
            **{
                env_name: identity.guami[key]
                for key, env_name in _GUAMI_KEYS.items()
            },
            "RC_SOURCE_CONNECTION_EPOCH": identity.epoch,
            "RC_SOURCE_NB_ID": self._nb_id,
        }
        return {key: str(value) for key, value in values.items()}

    @contextmanager
    def _invocation_header(self, values: Mapping[str, str]) -> Iterator[Path]:
        """Publish one exact header while the caller holds the ledger lock."""
        path: Optional[str] = None
        fd: Optional[int] = None
        try:
            fd, path = tempfile.mkstemp(
                prefix=".campaign5-control-", suffix=".env", dir=self._headers
            )
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                for key in (*_HEADER_KEYS, "RC_SOURCE_CONNECTION_EPOCH", "RC_SOURCE_NB_ID"):
                    handle.write(f"{key}={values[key]}\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            if path is not None:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            raise LiveWorkerRefusal(
                f"cannot materialize authoritative control header: {exc}"
            ) from exc
        try:
            yield Path(path)
        finally:
            if path is not None:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

    # -- policy helpers ---------------------------------------------------

    @staticmethod
    def _action(policy_type_id: str) -> _Action:
        try:
            return _ACTIONS[policy_type_id]
        except KeyError as exc:
            raise LiveWorkerRefusal(f"unsupported live policy type {policy_type_id}") from exc

    def _policy_values(self, action: _Action, policy: Mapping[str, Any]
                       ) -> tuple[dict[str, Any], str, Optional[str]]:
        if action.policy_type_id == SLICE_POLICY_TYPE_ID:
            quota = policy.get("quota")
            scope = policy.get("scope")
            if not isinstance(quota, Mapping) or not isinstance(scope, Mapping):
                raise LiveWorkerRefusal("slice policy lacks quota/scope")
            values = {field: quota[field] for field in action.value_fields}
            # Internal renderer-only value; never compared with KPM leaves.
            values["_scope"] = copy.deepcopy(dict(scope))
            return values, self._cell_id, None
        family: Campaign5Family = family_by_policy_type(action.policy_type_id)
        config = policy.get("config")
        if not isinstance(config, Mapping):
            raise LiveWorkerRefusal("Campaign-5 policy lacks config")
        values = {field: config[field] for field in family.value_fields}
        return values, str(config["cellId"]), (
            str(config["ueId"]) if family.scope_kind == "UE" else None
        )

    def _check_validity(self, policy: Mapping[str, Any]) -> None:
        validity = policy.get("validity")
        if not isinstance(validity, Mapping):
            raise LiveWorkerRefusal("policy lacks validity")
        try:
            before = self._parse_time(validity["notBefore"])
            after = self._parse_time(validity["notAfter"])
        except KeyError as exc:
            raise LiveWorkerRefusal("policy validity is incomplete") from exc
        now = self._clock()
        if now.tzinfo is None or not before <= now < after:
            raise LiveWorkerRefusal("policy is outside its validity window")

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise LiveWorkerRefusal("policy validity is not RFC3339") from exc
        if parsed.tzinfo is None:
            raise LiveWorkerRefusal("policy validity must carry a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _scope_key(action: _Action, identity: _ScopeIdentity, cell_id: str) -> str:
        if action.ue_scoped:
            if not isinstance(identity, _Identity):
                raise LiveWorkerError("UE action has a non-UE identity")
            target: Any = identity.amf_ue_ngap_id
        else:
            target = cell_id
        return f"action={action.action_id}/target={target}"

    @staticmethod
    def _identity_json(identity: _ScopeIdentity) -> dict[str, Any]:
        if isinstance(identity, _CellIdentity):
            return {
                "scopeKind": "CELL",
                "cellId": identity.cell_id,
                "nbId": identity.nb_id,
                "epoch": identity.epoch,
            }
        return {
            "scopeKind": "UE",
            "ueTag": identity.ue_tag,
            "amfUeNgapId": identity.amf_ue_ngap_id,
            "ranUeId": identity.ran_ue_id,
            "guami": dict(identity.guami),
            "epoch": identity.epoch,
        }

    @staticmethod
    def _identity_from_json(value: Mapping[str, Any]) -> _ScopeIdentity:
        if value.get("scopeKind") == "CELL":
            return _CellIdentity(
                str(value["cellId"]), int(value["nbId"]), int(value["epoch"])
            )
        return _Identity(
            str(value["ueTag"]), int(value["amfUeNgapId"]), int(value["ranUeId"]),
            {str(k): int(v) for k, v in value["guami"].items()}, int(value["epoch"]),
        )

    @staticmethod
    def _same_values(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
        public_expected = {key: value for key, value in expected.items() if not key.startswith("_")}
        return set(observed) == set(public_expected) and all(
            _same_number(observed[key], public_expected[key]) for key in observed
        )

    @staticmethod
    def _observed_config(action: _Action, policy: Mapping[str, Any],
                         observed: Mapping[str, Any]) -> Mapping[str, Any]:
        if action.policy_type_id == SLICE_POLICY_TYPE_ID:
            return dict(observed)
        config = dict(policy["config"])
        config.update(observed)
        return config


class Campaign5WorkerDispatcher:
    """One producer authority routing only to explicitly bound, fixed workers.

    Routing never consults serving-cell telemetry. Policy ids stay with their
    original ledger, including uncertain writes and failed withdrawals.
    """

    def __init__(self, workers: Sequence[Campaign5LiveWorker]) -> None:
        self._workers = tuple(workers)
        if not self._workers:
            raise LiveWorkerRefusal("at least one explicit cell binding is required")
        self._by_cell: dict[str, Campaign5LiveWorker] = {}
        paths: set[Path] = set()
        inodes: set[tuple[int, int]] = set()
        nodes: set[int] = set()
        for worker in self._workers:
            cell, node = worker._cell_id, worker._nb_id
            if not cell or cell.strip() != cell or len(cell) > 128:
                raise LiveWorkerRefusal("cell binding must be a nonblank deployment identity")
            if cell in self._by_cell or node in nodes:
                raise LiveWorkerRefusal("duplicate cell/node deployment binding")
            if node < 0 or node > 0xffffffff:
                raise LiveWorkerRefusal("node binding must be an unsigned 32-bit nbId")
            path = worker._ledger.path.resolve()
            inode = None
            if path.exists():
                stat = path.stat()
                inode = (stat.st_dev, stat.st_ino)
            if path in paths or (inode is not None and inode in inodes):
                raise LiveWorkerRefusal("cell workers must use distinct ledger paths")
            paths.add(path)
            if inode is not None:
                inodes.add(inode)
            nodes.add(node)
            self._by_cell[cell] = worker
        self._owners: dict[str, Campaign5LiveWorker] = {}
        self._producer: Any = None
        self._lifecycle_lock = threading.RLock()

    def bind(self, producer: Any) -> None:
        with producer.lifecycle_lock:
            if self._producer is not None:
                if self._producer is not producer:
                    raise LiveWorkerError("dispatcher is already bound to another producer")
                return
            owners: dict[str, Campaign5LiveWorker] = {}
            # All snapshots and schema/binding checks precede every recovery
            # callback. An invalid second ledger must not let the first write.
            for worker in self._workers:
                worker._check_attachment(producer)
                state = worker._ledger.snapshot()
                self._validate_ledger(worker, state, producer)
                for policy_id in state["entries"]:
                    if policy_id in owners:
                        raise LiveWorkerRefusal("duplicate policy id across cell ledgers")
                    owners[policy_id] = worker
            capability = producer.bind_live_worker(
                self._apply_from_a1, self._delete_from_a1,
                route_validator=self._validate_route,
            )
            self._producer = producer
            self._owners = owners
            for worker in self._workers:
                worker._attach(producer, capability)
        for worker in self._workers:
            worker._recover_producer_view()

    @staticmethod
    def _validate_ledger(worker: Campaign5LiveWorker, state: Mapping[str, Any],
                         producer: Any) -> None:
        entries, owners = state["entries"], state["owners"]
        if (entries or owners) and state.get("deploymentBinding") is None:
            raise LiveWorkerRefusal(
                "legacy ledger lacks cell/node deployment binding; multi-cell migration "
                "refused: keep the original single-cell lane or explicitly migrate "
                "with operator-verified original node provenance"
            )
        try:
            for policy_id, entry in entries.items():
                if not isinstance(policy_id, str) or not policy_id:
                    raise LiveWorkerRefusal("ledger policy id is malformed")
                action = worker._action(entry["policyTypeId"])
                if action.policy_type_id not in producer.get_policytypes():
                    raise LiveWorkerRefusal("ledger policy type is not advertised")
                identity = worker._identity_from_json(entry["identity"])
                if isinstance(identity, _CellIdentity) and (
                        identity.cell_id != worker._cell_id or identity.nb_id != worker._nb_id):
                    raise LiveWorkerRefusal("ledger identity differs from cell/node binding")
                if not isinstance(entry["baseline"], Mapping):
                    raise LiveWorkerRefusal("ledger has no persisted baseline")
                attempts = entry["attempts"]
                if not isinstance(attempts, Mapping) or entry["currentDigest"] not in attempts:
                    raise LiveWorkerRefusal("ledger has no current persisted attempt")
                for digest, attempt in attempts.items():
                    body = attempt["policy"]
                    producer._validate_policy(action.policy_type_id, body)
                    if body["config"]["cellId"] != worker._cell_id:
                        raise LiveWorkerRefusal("ledger policy cell differs from worker binding")
                    if jcs_sha256(body) != digest:
                        raise LiveWorkerRefusal("ledger policy digest differs from stored attempt")
                if owners.get(entry["scopeKey"]) == policy_id:
                    if worker._scope_key(action, identity, worker._cell_id) != entry["scopeKey"]:
                        raise LiveWorkerRefusal("ledger owner scope differs from persisted identity")
            for scope, policy_id in owners.items():
                if policy_id not in entries or entries[policy_id]["scopeKey"] != scope:
                    raise LiveWorkerRefusal("ledger owner has no matching persisted policy")
        except (KeyError, TypeError, ValueError, Campaign5Error) as exc:
            raise LiveWorkerRefusal("malformed cell-worker recovery ledger") from exc

    def _worker_for(self, policy: Mapping[str, Any]) -> Campaign5LiveWorker:
        from .producer import A1ValidationError
        config = policy.get("config")
        cell = config.get("cellId") if isinstance(config, Mapping) else None
        if not isinstance(cell, str) or not cell.strip() or cell not in self._by_cell:
            raise A1ValidationError("policy cellId has no explicit live worker binding")
        return self._by_cell[cell]

    def _validate_route(self, policy_type_id: str, policy_id: str,
                        policy: Mapping[str, Any]) -> None:
        from .producer import A1Conflict
        worker = self._worker_for(policy)
        owner = self._owners.get(policy_id)
        if owner is not None and owner is not worker:
            raise A1Conflict("policy id cannot move to another cell worker/ledger")

    def _apply_from_a1(self, policy_type_id: str, policy_id: str,
                       policy: Mapping[str, Any]) -> None:
        self._validate_route(policy_type_id, policy_id, policy)
        worker = self._worker_for(policy)
        had, previous = policy_id in self._owners, self._owners.get(policy_id)
        self._owners[policy_id] = worker
        try:
            worker._apply_from_a1(policy_type_id, policy_id, policy)
        except Exception:
            # 거절된 **생성**은 정책이 된 적이 없다.  프로듀서는 등록부를 되감는데
            # (`producer.py:314-338`) 여기 소유권 주장이 남으면 그 id 는 영영 다른
            # 셀에서 쓸 수 없고(`_validate_route` 가 A1Conflict 로 막는다) 맵도
            # 무한히 자란다.  **갱신** 실패는 실재하던 정책이므로 이전 소유자를
            # 그대로 되돌린다 -- 그 워커가 아직 롤백을 빚고 있을 수 있다.
            if had:
                self._owners[policy_id] = previous
            else:
                self._owners.pop(policy_id, None)
            raise

    def _delete_from_a1(self, policy_id: str) -> None:
        from .producer import A1Conflict
        record = self._producer.policy_record(policy_id)
        worker = self._owners.get(policy_id)
        if worker is None or worker is not self._worker_for(record["policy"]):
            raise A1Conflict("DELETE has no matching original cell-worker owner")
        worker._delete_from_a1(policy_id)

    def expire_due(self) -> tuple[str, ...]:
        """Each cell expires under its own lock; one cell's fault never stops another's."""
        if self._producer is None:
            return ()
        expired: list[str] = []
        for worker in self._workers:
            try:
                expired.extend(worker.expire_due())
            except Exception:  # noqa: BLE001 - logged; the next pass retries this cell
                _LOG.exception("Campaign-5 expiry pass failed for cell %s; "
                               "other cells continue", worker._cell_id)
        return tuple(expired)
