"""Read-only file configuration for the O1 PM and KPM source adapters."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Mapping, Tuple

from assurance.collector.o1col import KpmJsonlAdapter, O1PmFileAdapter
from assurance.contracts.live_binding import AssuranceLiveBinding

__all__ = ["KpmValidationReport", "LiveCollectorConfiguration", "build_live_collectors",
           "load_live_collector_configuration", "validate_kpm_jsonl"]


@dataclass(frozen=True)
class KpmValidationReport:
    path: str
    exists: bool
    state: str
    sample_count: int
    invalid_records: int
    lines_examined: int
    #: epoch 불일치로 **거부된 멀쩡한 실측**의 수.  `invalid_records`(입력이 깨짐)와
    #: 조치가 다르다 -- 이쪽이 오르면 재핀이 필요하다.  2026-09-21 에 두 수를 분리하면서
    #: 이 칸을 안 만들었더니, 모든 줄이 epoch 불일치인 파일이 `invalid_records == 0` 이라
    #: **샘플이 0 인데 상태가 VALID** 로 나왔다(침묵을 성공으로 읽은 것).
    missing_records: int = 0


@dataclass(frozen=True)
class LiveCollectorConfiguration:
    """The exact read-only paths and adapters bound for this deployment."""

    pm_directory: str
    kpm_jsonl_path: str
    o1_pm: O1PmFileAdapter
    kpm: KpmJsonlAdapter


def validate_kpm_jsonl(path: str | Path, *, expected_epochs: Mapping[str, int],
                       max_lines: int = 64) -> KpmValidationReport:
    """Read and parse only the first *max_lines* of a capture, read-only."""
    if isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines <= 0:
        raise ValueError("max_lines must be a positive integer")
    source = Path(path)
    if not source.is_file():
        return KpmValidationReport(str(source), False, "MISSING", 0, 0, 0)
    try:
        with source.open("r", encoding="utf-8") as stream:
            lines = tuple(islice(stream, max_lines))
            result = KpmJsonlAdapter(expected_epochs=expected_epochs, source_id="live-kpm").parse_lines(lines)
    except OSError:
        return KpmValidationReport(str(source), True, "UNREADABLE", 0, 0, 0)
    # 거부는 사유를 가리지 않고 전부 센다.  한 줄이라도 버려졌으면 VALID 가 아니고,
    # 쓸 수 있는 샘플이 하나도 없으면 PARTIAL 이다.
    rejected = result.invalid_records + result.missing_records
    # 2026-09-22: 거부가 0 이라고 해서 관측이 있었다는 뜻은 아니다.  복사 중인 0바이트
    # 파일이나 게이트가 죽어 비어 버린 capture 는 거부할 줄조차 없어 `VALID` 로 나왔다.
    # 침묵은 성공이 아니다 -- 빈 capture 는 제 이름으로 부른다.
    if not lines:
        state = "EMPTY"
    elif rejected == 0:
        state = "VALID" if result.samples else "EMPTY"
    else:
        state = "COMPATIBLE" if result.samples else "PARTIAL"
    return KpmValidationReport(str(source), True, state, len(result.samples),
                               result.invalid_records, len(lines),
                               result.missing_records)


def build_live_collectors(binding: AssuranceLiveBinding) -> Tuple[O1PmFileAdapter, KpmJsonlAdapter]:
    """Make file-only adapters; callers choose the PM paths to collect."""
    return (
        O1PmFileAdapter(read_bytes=lambda path: Path(path).read_bytes(), source_id="live-o1-pm"),
        KpmJsonlAdapter(expected_epochs=binding.kpm_expected_epochs, source_id="live-kpm"),
    )


def load_live_collector_configuration(binding: AssuranceLiveBinding) -> LiveCollectorConfiguration:
    """Carry the source paths beside their adapters; no directory is enumerated."""
    o1_pm, kpm = build_live_collectors(binding)
    return LiveCollectorConfiguration(
        pm_directory=binding.pm_directory, kpm_jsonl_path=binding.kpm_jsonl_path,
        o1_pm=o1_pm, kpm=kpm,
    )
