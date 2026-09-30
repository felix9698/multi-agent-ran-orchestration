"""A1 거절 종류만 담는 모듈 -- 전송을 하나도 import 하지 않는다.

2026-09-23 (codex 감사 Q14): `tools/liveconsole/build.py` 가 `A1Error` **하나** 때문에
`oran.campaign5.producer` 를 import 했고, producer 는 `live_worker` 를, live_worker 는
`our_rc_xapp`(E2SM-RC 제어 xApp)을 띄우려고 `subprocess` 를 import 한다.  그래서
`tests/test_oran_boundary.py` 의 최종 조립 폐포에 제어 전송이 들어왔다.

**확인된 것**: console 은 worker 를 만들지도 부르지도 않는다 -- 생성은
`producer.py:973` 과 그 파일의 `_main()` 안에 있다.  AST 가 함수 안의 import 까지
따라가서 생긴 **의존성** 경계 실패이지, 실행 중 R1 우회의 증거가 아니다.  그렇다고
가드를 느슨하게 하면 진짜 우회가 생겼을 때 조용해지므로, 거절 종류를 전송 없는 자리로
옮긴다.  `producer` 는 이 이름들을 그대로 다시 내보내므로 기존 import 는 안 깨진다.
"""
from __future__ import annotations

__all__ = [
    "A1CapabilityGate",
    "A1Conflict",
    "A1Error",
    "A1NotFound",
    "A1ValidationError",
]


class A1Error(ValueError):
    pass


class A1ValidationError(A1Error):
    pass


class A1Conflict(A1Error):
    pass


class A1NotFound(A1Error):
    pass


class A1CapabilityGate(A1Error):
    pass
