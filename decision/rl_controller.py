#!/usr/bin/env python3
"""
Trained tabular RL controller for the "rl_controller" baseline (Batch G, P0-20).

This is a GENUINE (if small) reinforcement-learning baseline, not a handcrafted
table: a one-step contextual-bandit tabular Q-learner trained offline against a
deterministic reward model of the RAN power->throughput response. It produces a
reproducible policy ARTIFACT with full training PROVENANCE and a content DIGEST,
so the paper can cite a trained policy whose training run is reproducible
byte-for-byte (same seed -> same digest).

Contract:
  * train_tabular_policy(cfg) runs seeded epsilon-greedy tabular Q-learning and
    returns a TrainedPolicy(policy table, Q-table, provenance, digest).
  * The reward model is DECLARED in the provenance (channel-gain estimate, I1
    bound, penalties) and hashed into an environment digest, so the learned
    policy is tied to the exact training environment.
  * Deterministic: identical cfg -> identical Q-table -> identical digest.
  * The policy maps a discretised throughput-deficit bucket to a power-offset
    action; the backend queries it greedily.

stdlib-only (random/hashlib/json/math), consistent with the rest of the repo.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

ARTIFACT_SCHEMA_VERSION = 2


class RLArtifactError(Exception):
    """Raised when a checked-in RL policy artifact fails fail-closed validation
    (bad schema, config mismatch, digest mismatch/corruption, or the training run
    does not reproduce the stored digest)."""


@dataclass(frozen=True)
class RLTrainingConfig:
    """Fully-declared, hashable training configuration (reward model + learner).

    The reward model is a DECLARED abstraction of the emulated RAN response:
    applying `offset` dB raises throughput by ~`channel_gain_db` per dB; the
    reward rewards closing the deficit while penalising exceeding the I1 power
    bound and wasting power. These constants are part of the provenance."""
    seed: int = 20260720
    n_episodes: int = 6000
    alpha: float = 0.2
    epsilon_start: float = 0.5
    epsilon_end: float = 0.02
    # state: throughput deficit discretised into n_deficit_buckets over [0, max]
    max_deficit_mbps: float = 8.0
    n_deficit_buckets: int = 16
    # action grid: candidate power offsets (dB)
    action_grid: Tuple[float, ...] = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5,
                                      4.0, 5.0, 6.0)
    # DECLARED reward model of the RAN power->throughput response
    channel_gain_db: float = 0.6        # Mbps per dB (matches ChannelModel.a)
    i1_power_bound_db: float = 3.0      # exceeding this is an I1 risk
    i1_penalty: float = 2.0             # penalty weight per dB over the bound
    overshoot_penalty: float = 0.15     # penalty for gain beyond the deficit
    power_cost: float = 0.05            # small cost per dB of offset

    def env_digest(self) -> str:
        body = {"channel_gain_db": self.channel_gain_db,
                "i1_power_bound_db": self.i1_power_bound_db,
                "i1_penalty": self.i1_penalty,
                "overshoot_penalty": self.overshoot_penalty,
                "power_cost": self.power_cost,
                "action_grid": list(self.action_grid),
                "max_deficit_mbps": self.max_deficit_mbps,
                "n_deficit_buckets": self.n_deficit_buckets}
        return "env-" + hashlib.sha256(
            json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["action_grid"] = list(self.action_grid)
        return d


@dataclass
class TrainedPolicy:
    """The trained artifact: greedy policy + Q-table + provenance + digest."""
    policy: Dict[int, float]                # deficit bucket -> chosen offset (dB)
    q_table: Dict[int, List[float]]         # bucket -> Q value per action
    provenance: Dict
    digest: str

    def action_for_deficit(self, deficit_mbps: float,
                           cfg: RLTrainingConfig) -> float:
        """Greedy learned action for a throughput deficit (dB power offset)."""
        b = _deficit_bucket(deficit_mbps, cfg)
        return float(self.policy.get(b, 0.0))

    def to_dict(self) -> Dict:
        return {"policy": {str(k): v for k, v in self.policy.items()},
                "q_table": {str(k): v for k, v in self.q_table.items()},
                "provenance": self.provenance, "digest": self.digest}

    @staticmethod
    def from_dict(d: Dict) -> "TrainedPolicy":
        return TrainedPolicy(
            policy={int(k): float(v) for k, v in d["policy"].items()},
            q_table={int(k): list(v) for k, v in d["q_table"].items()},
            provenance=d["provenance"], digest=d["digest"])


def _deficit_bucket(deficit_mbps: float, cfg: RLTrainingConfig) -> int:
    d = max(0.0, min(cfg.max_deficit_mbps, float(deficit_mbps)))
    width = cfg.max_deficit_mbps / cfg.n_deficit_buckets
    if width <= 0:
        return 0
    return min(cfg.n_deficit_buckets - 1, int(d / width))


def _reward(deficit: float, offset: float, cfg: RLTrainingConfig) -> float:
    """DECLARED reward: close the deficit, penalise I1-bound breach / waste."""
    gain = cfg.channel_gain_db * offset
    new_deficit = max(0.0, deficit - gain)
    i1_pen = cfg.i1_penalty * max(0.0, offset - cfg.i1_power_bound_db)
    overshoot = cfg.overshoot_penalty * max(0.0, gain - deficit)
    return -(new_deficit) - i1_pen - overshoot - cfg.power_cost * offset


def train_tabular_policy(cfg: Optional[RLTrainingConfig] = None) -> TrainedPolicy:
    """Seeded epsilon-greedy tabular Q-learning (one-step contextual bandit).
    Deterministic: same cfg -> same Q-table -> same digest."""
    cfg = cfg or RLTrainingConfig()
    rng = random.Random(f"rl:{cfg.seed}:{cfg.env_digest()}")
    n_actions = len(cfg.action_grid)
    q: Dict[int, List[float]] = {b: [0.0] * n_actions
                                 for b in range(cfg.n_deficit_buckets)}
    counts: Dict[int, List[int]] = {b: [0] * n_actions
                                    for b in range(cfg.n_deficit_buckets)}
    for ep in range(cfg.n_episodes):
        # sample a random deficit state (the exogenous context)
        deficit = rng.uniform(0.0, cfg.max_deficit_mbps)
        b = _deficit_bucket(deficit, cfg)
        frac = ep / max(1, cfg.n_episodes - 1)
        eps = cfg.epsilon_start + (cfg.epsilon_end - cfg.epsilon_start) * frac
        if rng.random() < eps:
            a = rng.randrange(n_actions)
        else:
            row = q[b]
            best = max(row)
            # deterministic tie-break: lowest action index (=> smallest offset)
            a = row.index(best)
        r = _reward(deficit, cfg.action_grid[a], cfg)
        counts[b][a] += 1
        q[b][a] += cfg.alpha * (r - q[b][a])
    # greedy policy: argmax action per bucket (smallest-offset tie-break)
    policy: Dict[int, float] = {}
    for b in range(cfg.n_deficit_buckets):
        row = q[b]
        best = max(row)
        a = row.index(best)
        policy[b] = float(cfg.action_grid[a])
    provenance = {
        "algorithm": "tabular_q_learning_one_step_contextual_bandit",
        "learned": True,
        "config": cfg.to_dict(),
        "env_digest": cfg.env_digest(),
        "n_episodes": cfg.n_episodes,
        "n_states": cfg.n_deficit_buckets,
        "n_actions": n_actions,
        "state_action_visit_counts_total": sum(
            sum(row) for row in counts.values()),
    }
    digest = _policy_digest(policy, q, provenance)
    return TrainedPolicy(policy=policy, q_table=q, provenance=provenance,
                         digest=digest)


def _policy_digest(policy: Dict[int, float], q: Dict[int, List[float]],
                   provenance: Dict) -> str:
    """Canonical SHA-256 over the rounded policy + Q-table + provenance."""
    body = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "policy": {str(k): round(v, 6) for k, v in sorted(policy.items())},
        "q": {str(k): [round(x, 6) for x in q[k]] for k in sorted(q)},
        "provenance": provenance,
    }
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
    return "rlpolicy-sha256-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


DEFAULT_ARTIFACT_PATH = None  # set below to decision/rl_tabular_policy.json


def save_artifact(policy: TrainedPolicy, path: str) -> str:
    payload = policy.to_dict()
    payload["schema_version"] = ARTIFACT_SCHEMA_VERSION
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True, allow_nan=False)
    return path


_REQUIRED_TOP = ("policy", "q_table", "provenance", "digest")
_REQUIRED_PROV = ("algorithm", "learned", "config", "env_digest", "n_episodes")


def load_artifact(path: str, cfg: Optional[RLTrainingConfig] = None,
                  verify_reproduction: bool = True) -> TrainedPolicy:
    """Load a checked-in policy artifact with FAIL-CLOSED validation:
      1. schema: required top-level + provenance keys present;
      2. digest: recomputed canonical SHA-256 must equal the stored digest
         (detects any corruption/tamper);
      3. config: provenance.config must match `cfg` (env/config match);
      4. reproduction (default): re-training with `cfg` must yield the SAME
         digest (the artifact is a faithful record of a reproducible run).
    Any failure raises RLArtifactError - never a silently-wrong policy."""
    try:
        with open(path, "r") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        raise RLArtifactError(f"cannot read RL artifact {path!r}: {e}")
    if not isinstance(d, dict) or any(k not in d for k in _REQUIRED_TOP):
        raise RLArtifactError(f"RL artifact {path!r} missing required keys "
                              f"{_REQUIRED_TOP}")
    prov = d.get("provenance")
    if not isinstance(prov, dict) or any(k not in prov for k in _REQUIRED_PROV):
        raise RLArtifactError(f"RL artifact {path!r} provenance missing "
                              f"{_REQUIRED_PROV}")
    if not prov.get("learned"):
        raise RLArtifactError("RL artifact is not marked learned")
    try:
        tp = TrainedPolicy.from_dict(d)
    except (KeyError, TypeError, ValueError) as e:
        raise RLArtifactError(f"RL artifact {path!r} malformed: {e}")
    recomputed = _policy_digest(tp.policy, tp.q_table, tp.provenance)
    if recomputed != tp.digest:
        raise RLArtifactError(
            f"RL artifact {path!r} DIGEST MISMATCH (corruption/tamper): "
            f"stored {tp.digest!r} != recomputed {recomputed!r}")
    cfg = cfg or RLTrainingConfig()
    if prov.get("config") != cfg.to_dict():
        raise RLArtifactError(
            f"RL artifact {path!r} config does not match the expected "
            f"training config (env/hyperparameters differ)")
    if prov.get("env_digest") != cfg.env_digest():
        raise RLArtifactError(f"RL artifact {path!r} env_digest mismatch")
    if verify_reproduction:
        fresh = train_tabular_policy(cfg)
        if fresh.digest != tp.digest:
            raise RLArtifactError(
                f"RL artifact {path!r} does NOT reproduce: retraining yields "
                f"{fresh.digest!r} != stored {tp.digest!r}")
    return tp


# Process-level cache so a coordinator/backend trains/loads the policy once.
_CACHED: Dict[str, TrainedPolicy] = {}


def get_trained_policy(cfg: Optional[RLTrainingConfig] = None,
                       artifact_path: Optional[str] = None) -> TrainedPolicy:
    """Return the trained policy, preferring a validated CHECKED-IN artifact.

    If `artifact_path` (default: the packaged decision/rl_tabular_policy.json)
    exists it is LOADED and fully validated (fail closed); otherwise the policy
    is trained deterministically and saved so the artifact exists next time."""
    cfg = cfg or RLTrainingConfig()
    import os
    if artifact_path is None:
        artifact_path = _default_artifact_path()
    key = f"{cfg.seed}:{cfg.env_digest()}:{cfg.n_episodes}:{artifact_path}"
    if key in _CACHED:
        return _CACHED[key]
    if artifact_path and os.path.isfile(artifact_path):
        tp = load_artifact(artifact_path, cfg=cfg, verify_reproduction=True)
    else:
        tp = train_tabular_policy(cfg)
        if artifact_path:
            try:
                save_artifact(tp, artifact_path)
            except OSError:
                pass
    _CACHED[key] = tp
    return tp


def _default_artifact_path() -> str:
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "rl_tabular_policy.json")


if __name__ == "__main__":
    cfg = RLTrainingConfig()
    p = train_tabular_policy(cfg)
    out = _default_artifact_path()
    save_artifact(p, out)
    # prove the just-written artifact loads + validates + reproduces
    load_artifact(out, cfg=cfg, verify_reproduction=True)
    print("digest:", p.digest)
    print("policy:", p.policy)
    print("saved + validated:", out)
