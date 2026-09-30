"""`FileEventStore.append` 는 파일 전체를 다시 읽지 않는다.

예전에는 append 마다 파일 **전체**를 재구성하고 각 이벤트의 content hash 를 다시 검증했다.
N 번째 append 가 N 건을 JCS 정규화하므로 총 O(N**2) 이다.  2026-09-23 04:05 에 판 하나가
이벤트 5,826건에서 **20분간 CPU 78%로 멎었고**, 스택은 두 번 다
`append -> _event_from_record -> payload_digest -> jcs.canonicalize` 였다.

보장은 줄지 않아야 한다: flock 아래에서 다른 쓰기 주체가 덧붙인 기록도 접어 넣고,
순번·중복·멱등키는 **전체 상태**에 대해 검사한다.
"""
import json
import tempfile
import unittest
from pathlib import Path

from assurance.core.components import ComponentId
from assurance.core.envelopes import EventEnvelope
from assurance.core.addressing import content_hash as digest_of
from assurance.kernel.event_store import EventStoreError, FileEventStore


def _envelope(sequence: int, *, object_id: str = "case/x", payload=None) -> EventEnvelope:
    body = payload if payload is not None else {"n": sequence}
    return EventEnvelope(
        schema_version="assurance-event/1",
        object_id=object_id,
        event_id=f"event/{object_id}/{sequence}",
        timestamp="2026-09-23T00:00:00.000000Z",
        content_hash=digest_of(body),
        sequence=sequence,
        expiry=None,
        idempotency_key=f"key/{object_id}/{sequence}",
        source_component=ComponentId.ASSURANCE_KERNEL,
        payload=body,
        event_kind="RawSampleIngested",
    )


class TestAppendReadsOnlyWhatIsNew(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "events.jsonl"

    def test_appends_are_recorded_in_order(self):
        store = FileEventStore(self.path)
        for n in range(0, 5):
            self.assertEqual(store.append(_envelope(n)), n)
        self.assertEqual([e.sequence for e in store.iterate()], [0, 1, 2, 3, 4])
        self.assertEqual(self.path.read_text().count("\n"), 5)

    def test_the_work_per_append_does_not_grow_with_the_log(self):
        """줄을 두 번 읽지 않는다 -- 읽은 바이트로 센다."""
        store = FileEventStore(self.path)
        seen = []
        original = store._fold_appended_records

        def counting(descriptor):
            before = store._consumed_bytes
            original(descriptor)
            seen.append(store._consumed_bytes - before)
        store._fold_appended_records = counting
        for n in range(0, 20):
            store.append(_envelope(n))
        # 이 저장소가 유일한 쓰기 주체이므로 append 가 새로 읽을 바이트는 0이어야 한다
        self.assertEqual(set(seen), {0}, f"append 가 다시 읽은 바이트: {seen}")

    def test_a_record_another_writer_appended_is_folded_in(self):
        """보장이 줄면 안 된다 -- 남이 붙인 것도 잡아야 순번 검사가 참이다."""
        store = FileEventStore(self.path)
        store.append(_envelope(0))
        # 다른 주체가 직접 덧붙인다
        other = FileEventStore(self.path)
        other.append(_envelope(1))
        # 우리 저장소는 아직 1을 모르지만, 다음 append 에서 접어 넣고 2를 요구해야 한다
        with self.assertRaises(EventStoreError):
            store.append(_envelope(1))          # 순번 중복
        self.assertEqual(store.append(_envelope(2)), 2)
        self.assertEqual([e.sequence for e in store.iterate()], [0, 1, 2])

    def test_a_truncated_final_record_is_refused(self):
        store = FileEventStore(self.path)
        store.append(_envelope(0))
        with self.path.open("a") as handle:
            handle.write('{"partial": true')      # 줄바꿈 없음
        with self.assertRaises(EventStoreError):
            store.append(_envelope(1))

    def test_a_shrinking_log_is_refused_rather_than_silently_reread(self):
        store = FileEventStore(self.path)
        for n in range(0, 3):
            store.append(_envelope(n))
        self.path.write_text("")                  # 원장은 추가 전용이다
        with self.assertRaises(EventStoreError):
            store.append(_envelope(3))

    def test_reopening_reads_everything_once(self):
        store = FileEventStore(self.path)
        for n in range(0, 3):
            store.append(_envelope(n))
        again = FileEventStore(self.path)
        self.assertEqual([e.sequence for e in again.iterate()], [0, 1, 2])
        self.assertEqual(again._consumed_bytes, self.path.stat().st_size)

    def test_a_corrupt_line_from_another_writer_is_named(self):
        store = FileEventStore(self.path)
        store.append(_envelope(0))
        with self.path.open("a") as handle:
            handle.write("not json\n")
        with self.assertRaises(EventStoreError):
            store.append(_envelope(1))


if __name__ == "__main__":
    unittest.main()
