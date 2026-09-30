"""Structural mirror of the frozen ``oran/release/ubm/service.py::UbmEndpoints``.

The self-test may not import the upper runtime (DESIGN §11), so it carries its
own dataclass with the same member names and derives it from the deployment
vector exactly as ``work-split.1.0.0.json#/frozenInterfaces`` freezes it.  Any
object exposing ``r1``/``rapp``/``o1_provider``/``o1_consumer``/``lower_a1`` is
accepted by the double, so the real runtime's dataclass drops in unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .wire import origin_of


@dataclass(frozen=True)
class UbmEndpoints:
    r1: str
    rapp: str
    o1_provider: str
    o1_consumer: str
    lower_a1: str

    @classmethod
    def from_vector(cls, vector: Mapping[str, Any]) -> "UbmEndpoints":
        file_reporting = vector["o1"]["fileDataReporting"]
        return cls(
            r1=origin_of(vector["r1"]["apiRoot"]),
            rapp=origin_of(vector["r1"]["callbackApi"]["rootUri"]),
            o1_provider=origin_of(file_reporting["mnsRoot"]),
            o1_consumer=origin_of(file_reporting["consumerReference"]),
            lower_a1=origin_of(vector["a1"]["apiRoot"]),
        )

    def upper_origins(self) -> tuple[str, ...]:
        return (self.r1, self.rapp, self.o1_provider, self.o1_consumer)
