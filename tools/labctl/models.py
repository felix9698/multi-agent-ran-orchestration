"""Immutable inventory models for labctl."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple


@dataclass(frozen=True)
class HostSpec:
    id: str
    transport: str
    target: Optional[str] = None


@dataclass(frozen=True)
class CommandSpec:
    argv: Tuple[str, ...]
    timeout_seconds: int = 60


@dataclass(frozen=True)
class ComponentSpec:
    id: str
    host: str
    stage: int
    dependencies: Tuple[str, ...]
    rf: bool
    stop_policy: str
    status: CommandSpec
    start: Optional[CommandSpec]
    stop: Optional[CommandSpec]
    configure: Optional[CommandSpec]
    readiness_category: str
    required: bool
    inventory_index: int


@dataclass(frozen=True)
class LabInventory:
    schema_version: str
    profile_id: str
    objective: str
    state_root: Path
    hosts: Mapping[str, HostSpec]
    components: Tuple[ComponentSpec, ...]

    def component(self, component_id: str) -> ComponentSpec:
        for component in self.components:
            if component.id == component_id:
                return component
        raise KeyError(component_id)

    def ordered_components(self) -> Tuple[ComponentSpec, ...]:
        """Return a stable topological order, using stage as the priority."""
        by_id = {component.id: component for component in self.components}
        remaining = {
            component.id: set(component.dependencies) for component in self.components
        }
        ordered = []
        while remaining:
            ready = [
                by_id[component_id]
                for component_id, dependencies in remaining.items()
                if not dependencies
            ]
            if not ready:
                raise ValueError("dependency cycle")
            ready.sort(key=lambda item: (item.stage, item.inventory_index))
            for component in ready:
                ordered.append(component)
                del remaining[component.id]
                for dependencies in remaining.values():
                    dependencies.discard(component.id)
        return tuple(ordered)
