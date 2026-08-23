"""Configuration: capacity, shifts, SLA, and cost assumptions.

The cost assumptions are inherited from ``fraud-threshold-lab`` so the two
projects agree: a fully loaded analyst costs $35/hour, a manual review takes
4 minutes, and a false alarm carries $5 of customer friction on top of the
review labour. A missed fraud costs the transaction amount.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

RANDOM_SEED = 20260809

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
ARTIFACTS = ROOT / "artifacts"
FIGURES = ARTIFACTS / "figures"
TABLES = ARTIFACTS / "tables"

SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class CapacityConfig:
    """Review capacity, modelled as a rate available during shift hours.

    Individual analysts are not simulated as agents. What matters to every
    question asked here is how many reviews per hour the operation can absorb
    and when, and a rate captures that without inventing per-analyst behaviour
    that the data cannot support.
    """

    n_analysts: int = 3
    minutes_per_review: float = 4.0
    shift_start_hour: float = 8.0
    shift_hours_per_day: float = 8.0
    days_per_week: int = 7
    analyst_hourly_cost: float = 35.0

    @property
    def reviews_per_analyst_hour(self) -> float:
        return 60.0 / self.minutes_per_review

    @property
    def reviews_per_hour(self) -> float:
        return self.n_analysts * self.reviews_per_analyst_hour

    @property
    def reviews_per_day(self) -> float:
        return self.reviews_per_hour * self.shift_hours_per_day

    @property
    def shift_end_hour(self) -> float:
        return self.shift_start_hour + self.shift_hours_per_day

    def is_on_shift(self, hour_of_day: float) -> bool:
        return self.shift_start_hour <= hour_of_day < self.shift_end_hour


@dataclass(frozen=True)
class SLAConfig:
    """Review deadline, escalation point, and the capacity reserved for escalations.

    ``escalation_capacity_share`` is load-bearing and easy to overlook. If every
    review slot serves the escalated tier whenever one is waiting, then on a
    backlogged queue -- where essentially every alert ages past the escalation
    point before anyone reaches it -- the escalated tier contains the entire
    queue in arrival order, and the policy silently degenerates into FIFO.
    Measured on the synthetic stream, that is exactly what happens: an unshared
    escalation tier reproduces FIFO's numbers to the dollar.

    Reserving a fixed share instead means ageing bounds starvation without
    destroying the risk ordering, which is how a real triage desk runs: one
    analyst works the ageing tail while the rest work the highest-risk items.
    """

    deadline_hours: float = 24.0
    escalate_after_hours: float = 12.0
    escalation_capacity_share: float = 1.0 / 3.0


@dataclass(frozen=True)
class CostConfig:
    """Matches fraud-threshold-lab so the two projects report comparable dollars."""

    analyst_hourly_cost: float = 35.0
    minutes_per_review: float = 4.0
    friction_cost_per_fp: float = 5.0

    @property
    def review_cost_per_alert(self) -> float:
        return self.analyst_hourly_cost * (self.minutes_per_review / 60.0)

    @property
    def cost_per_false_positive(self) -> float:
        return self.review_cost_per_alert + self.friction_cost_per_fp


@dataclass(frozen=True)
class Config:
    capacity: CapacityConfig = field(default_factory=CapacityConfig)
    sla: SLAConfig = field(default_factory=SLAConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    seed: int = RANDOM_SEED


DEFAULT = Config()


def ensure_dirs() -> None:
    for d in (DATA, ARTIFACTS, FIGURES, TABLES):
        d.mkdir(parents=True, exist_ok=True)
