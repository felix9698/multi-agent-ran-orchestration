#!/usr/bin/env python3
"""
Intent Model for Multi-UE Intent Coordination

Implements 3GPP TS 28.312 intent structure with extensions for:
- Per-UE intent specification
- Cross-UE KPI coupling
- Multi-parameter action space
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional, Any
import uuid


class IntentType(Enum):
    """Intent types (as per Extension Strategy)"""
    THROUGHPUT_GOAL = "throughput_goal"
    THROUGHPUT_FAIRNESS = "throughput_fairness"
    LATENCY_GOAL = "latency_goal"
    POWER_CONSTRAINT = "power_constraint"
    RSRP_CONSTRAINT = "rsrp_constraint"
    ENERGY_EFFICIENCY = "energy_efficiency"
    COVERAGE_GOAL = "coverage_goal"


class ConstraintType(Enum):
    """Constraint types for intent targets"""
    MIN = "min"           # value >= target
    MAX = "max"           # value <= target
    RANGE = "range"       # min <= value <= max
    EQUAL = "equal"       # value == target


class IntentStatus(Enum):
    """Intent lifecycle status"""
    PENDING = auto()      # Awaiting evaluation
    ACTIVE = auto()       # Being fulfilled
    SATISFIED = auto()    # Target achieved
    VIOLATED = auto()     # Target not met
    NEGOTIATING = auto()  # In negotiation
    WITHDRAWN = auto()    # User withdrew


class IntentPriority(Enum):
    """Intent priority levels"""
    CRITICAL = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4


@dataclass
class IntentTarget:
    """Intent target specification"""
    kpi_name: str                    # e.g., "throughput", "latency", "rsrp"
    constraint_type: ConstraintType
    target_value: float
    min_value: Optional[float] = None  # For RANGE constraint
    max_value: Optional[float] = None  # For RANGE constraint
    unit: str = ""                     # e.g., "Mbps", "ms", "dBm"

    def is_satisfied(self, value: float) -> bool:
        """Check if value satisfies this target"""
        if self.constraint_type == ConstraintType.MIN:
            return value >= self.target_value
        elif self.constraint_type == ConstraintType.MAX:
            return value <= self.target_value
        elif self.constraint_type == ConstraintType.RANGE:
            return (self.min_value <= value <= self.max_value)
        elif self.constraint_type == ConstraintType.EQUAL:
            return abs(value - self.target_value) < 0.01
        return False


@dataclass
class IntentScope:
    """Intent scope specification (which UEs/BSs it applies to)"""
    ue_ids: List[str] = field(default_factory=list)     # Empty = all UEs
    bs_ids: List[str] = field(default_factory=list)     # Empty = all BSs
    cell_ids: List[int] = field(default_factory=list)   # Empty = all cells
    area_id: Optional[str] = None                       # Geographic area


@dataclass
class Intent:
    """
    Intent definition following 3GPP TS 28.312 structure.

    An intent specifies WHAT the operator wants (declarative),
    not HOW to achieve it (imperative).
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    type: IntentType = IntentType.THROUGHPUT_GOAL
    target: IntentTarget = None
    scope: IntentScope = field(default_factory=IntentScope)
    priority: IntentPriority = IntentPriority.MEDIUM
    status: IntentStatus = IntentStatus.PENDING

    # Metadata
    description: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None

    # Current state
    current_value: Optional[float] = None
    last_checked: Optional[datetime] = None

    # For backwards compatibility
    target_value: float = 0.0
    constraint_type: ConstraintType = ConstraintType.MIN

    def __post_init__(self):
        if self.target is None:
            self.target = IntentTarget(
                kpi_name=self._get_kpi_name(),
                constraint_type=self.constraint_type,
                target_value=self.target_value
            )

    def _get_kpi_name(self) -> str:
        """Map intent type to KPI name"""
        mapping = {
            IntentType.THROUGHPUT_GOAL: "throughput",
            IntentType.THROUGHPUT_FAIRNESS: "throughput_variance",
            IntentType.LATENCY_GOAL: "latency",
            IntentType.POWER_CONSTRAINT: "tx_power",
            IntentType.RSRP_CONSTRAINT: "rsrp",
            IntentType.ENERGY_EFFICIENCY: "power_consumption",
            IntentType.COVERAGE_GOAL: "coverage_ratio"
        }
        return mapping.get(self.type, "unknown")

    def is_satisfied(self, value: float = None) -> bool:
        """Check if intent is satisfied"""
        check_value = value if value is not None else self.current_value
        if check_value is None:
            return False
        return self.target.is_satisfied(check_value)

    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            "id": self.id,
            "type": self.type.value,
            "target": {
                "kpi_name": self.target.kpi_name,
                "constraint_type": self.target.constraint_type.value,
                "target_value": self.target.target_value,
                "unit": self.target.unit
            },
            "scope": {
                "ue_ids": self.scope.ue_ids,
                "bs_ids": self.scope.bs_ids
            },
            "priority": self.priority.value,
            "status": self.status.name,
            "description": self.description,
            "current_value": self.current_value
        }

    def __str__(self) -> str:
        scope_str = ""
        if self.scope.ue_ids:
            scope_str = f" (UE: {','.join(self.scope.ue_ids)})"
        return (f"Intent[{self.id}] {self.type.value}: "
                f"{self.target.constraint_type.value} {self.target.target_value}"
                f"{scope_str}")


@dataclass
class Action:
    """Action to be executed on RAN"""
    command: str                    # e.g., "cell_gain", "prb_allocation"
    target_id: str                  # e.g., "bs1", "ue1"
    cell_id: int = 0
    value: float = 0.0
    description: str = ""

    # For backwards compatibility
    enb_id: str = ""

    def __post_init__(self):
        if not self.enb_id:
            self.enb_id = self.target_id

    def to_dict(self) -> Dict:
        return {
            "command": self.command,
            "target_id": self.target_id,
            "cell_id": self.cell_id,
            "value": self.value,
            "description": self.description
        }


@dataclass
class Alternative:
    """Alternative intent/action for negotiation"""
    id: str
    description: str
    actions: List[Action] = field(default_factory=list)
    modified_intent: Optional[Intent] = None
    expected_results: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.5

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "description": self.description,
            "actions": [a.to_dict() for a in self.actions],
            "expected_results": self.expected_results,
            "confidence": self.confidence
        }


@dataclass
class NetworkState:
    """
    Current network state including per-UE KPIs.

    Extended for Multi-UE with cross-UE coupling tracking.
    """
    timestamp: datetime = field(default_factory=datetime.now)

    # Per-UE state
    ue_states: Dict[str, Dict] = field(default_factory=dict)

    # Per-BS state
    bs_states: Dict[str, Dict] = field(default_factory=dict)

    # Global state (backwards compatibility)
    attached: bool = False
    ip_address: Optional[str] = None
    serving_pci: Optional[int] = None
    rsrp: Optional[float] = None
    rsrq: Optional[float] = None
    sinr: Optional[float] = None
    throughput: Optional[float] = None
    latency: Optional[float] = None
    power: Optional[float] = None
    tx_gain: Optional[float] = None

    # Cell gains
    cell_gains: Dict[int, float] = field(default_factory=dict)
    neighbors: List[Dict] = field(default_factory=list)

    def get_ue_state(self, ue_id: str) -> Dict:
        """Get state for specific UE"""
        return self.ue_states.get(ue_id, {})

    def set_ue_state(self, ue_id: str, state: Dict):
        """Set state for specific UE"""
        self.ue_states[ue_id] = state

    def get_bs_state(self, bs_id: str) -> Dict:
        """Get state for specific BS"""
        return self.bs_states.get(bs_id, {})

    def set_bs_state(self, bs_id: str, state: Dict):
        """Set state for specific BS"""
        self.bs_states[bs_id] = state

    @staticmethod
    def _ue_throughput(ue_state: Dict) -> Optional[float]:
        """UE state dicts use 'throughput_mbps' (UEMetrics.to_dict)"""
        value = ue_state.get("throughput_mbps")
        if value is None:
            value = ue_state.get("throughput")
        return value

    def get_aggregate_throughput(self) -> float:
        """Get total throughput across all UEs"""
        total = 0.0
        for ue_state in self.ue_states.values():
            value = self._ue_throughput(ue_state)
            if value is not None:
                total += value
        return total

    def get_throughput_variance(self) -> float:
        """Get throughput variance across UEs (for fairness)"""
        throughputs = [self._ue_throughput(s) for s in self.ue_states.values()]
        throughputs = [t for t in throughputs if t is not None]
        if len(throughputs) < 2:
            return 0.0
        mean = sum(throughputs) / len(throughputs)
        variance = sum((t - mean) ** 2 for t in throughputs) / len(throughputs)
        return variance

    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            "timestamp": self.timestamp.isoformat(),
            "ue_states": self.ue_states,
            "bs_states": self.bs_states,
            "global": {
                "attached": self.attached,
                "serving_pci": self.serving_pci,
                "rsrp": self.rsrp,
                "throughput": self.throughput,
                "latency": self.latency
            },
            "cell_gains": self.cell_gains
        }


@dataclass
class FeasibilityPrediction:
    """LLM feasibility prediction result"""
    feasible: bool
    confidence: float
    reasoning: str
    proposed_config: Dict[str, float] = field(default_factory=dict)
    expected_kpi: Dict[str, float] = field(default_factory=dict)
    alternatives: List[Alternative] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Post-trial validation result"""
    all_intents_satisfied: bool
    intent_results: Dict[str, bool] = field(default_factory=dict)
    actual_values: Dict[str, float] = field(default_factory=dict)
    kpi_delta: Dict[str, float] = field(default_factory=dict)


@dataclass
class NegotiationResult:
    """Negotiation outcome"""
    user_choice: str
    selected_alternative: Optional[Alternative] = None
    negotiation_rounds: int = 0
    accepted: bool = False


@dataclass
class ResolutionRecord:
    """Resolution episode record for history reservoir"""
    session_id: str
    timestamp: datetime = field(default_factory=datetime.now)
    intent_ids: List[str] = field(default_factory=list)
    resolution_type: str = ""
    conflict_type: str = ""
    success: bool = False
    kpi_before: Dict = field(default_factory=dict)
    kpi_after: Dict = field(default_factory=dict)
    actions_taken: List[Action] = field(default_factory=list)
    rolled_back: bool = False
    duration_ms: float = 0
    prediction_accuracy: Optional[float] = None

    # Per-UE tracking
    per_ue_results: Dict[str, Dict] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "session_id": self.session_id,
            "timestamp": self.timestamp.isoformat(),
            "intent_ids": self.intent_ids,
            "resolution_type": self.resolution_type,
            "conflict_type": self.conflict_type,
            "success": self.success,
            "kpi_before": self.kpi_before,
            "kpi_after": self.kpi_after,
            "actions": [a.to_dict() for a in self.actions_taken],
            "rolled_back": self.rolled_back,
            "duration_ms": self.duration_ms,
            "prediction_accuracy": self.prediction_accuracy,
            "per_ue_results": self.per_ue_results
        }
