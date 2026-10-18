"""Discrete-event review queue simulator.

Alerts arrive at their transaction timestamps. A review operation with finite
capacity works them off. Which alert gets picked next is the policy, and the
policy is the thing under test.

The simulation answers a question that a static threshold analysis cannot: a
threshold sweep says "at this cutoff you get 11,384 alerts needing 759 analyst
hours", but it silently assumes every alert eventually gets reviewed. A real
queue with finite capacity does not clear, and *what you review first* then
decides how much fraud you actually catch and how long it takes to catch it.

Three policies are implemented:

``FIFO``
    First in, first out. The baseline manual process, and what most operations
    do by default.
``RISK_RANKED``
    Highest score first. Maximises fraud caught per review, at the cost of
    letting low-scoring alerts sit indefinitely.
``RISK_RANKED_AGING``
    Highest score first, except that a reserved share of review capacity is
    spent on whichever aged alert has waited longest. Bounds the starvation
    that pure risk ranking creates, and is what an operation with an SLA would
    actually run. The reserved share matters: see
    :class:`~triage.config.SLAConfig` for why an unshared escalation tier
    collapses back into FIFO on a backlogged queue.

Reviews are assumed perfect: reviewing an alert reveals its true label. That is
a deliberate simplification -- see the README's limitations. It keeps the policy
comparison attributable to queue order and capacity alone, rather than to a
second invented error model layered on top of an already-assumed cost model.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

from .config import SECONDS_PER_DAY, SECONDS_PER_HOUR, CapacityConfig, SLAConfig


class Policy(str, Enum):
    FIFO = "fifo"
    RISK_RANKED = "risk_ranked"
    RISK_RANKED_AGING = "risk_ranked_aging"


@dataclass
class SimulationResult:
    """Per-alert outcomes plus the queue-depth trace."""

    policy: Policy
    alerts: pd.DataFrame
    queue_trace: pd.DataFrame
    capacity: CapacityConfig
    sla: SLAConfig
    review_slots: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def reviewed(self) -> pd.DataFrame:
        return self.alerts[self.alerts["reviewed"]]

    @property
    def unreviewed(self) -> pd.DataFrame:
        return self.alerts[~self.alerts["reviewed"]]


def generate_review_slots(
    start_dt: float,
    end_dt: float,
    capacity: CapacityConfig,
    capacity_scale: float = 1.0,
) -> np.ndarray:
    """Timestamps at which a review can be performed.

    Capacity is emitted as evenly spaced slots inside each day's shift window.
    ``capacity_scale`` multiplies the rate, which is how the capacity-headroom
    sweep explores staffing levels without rebuilding the shift model.
    """
    if capacity_scale < 0:
        raise ValueError(f"capacity_scale must be non-negative, got {capacity_scale}.")
    per_hour = capacity.reviews_per_hour * capacity_scale
    if per_hour <= 0:
        return np.empty(0, dtype=float)

    slots: list[np.ndarray] = []
    first_day = int(np.floor(start_dt / SECONDS_PER_DAY))
    last_day = int(np.floor(end_dt / SECONDS_PER_DAY))

    n_per_day = int(round(per_hour * capacity.shift_hours_per_day))
    if n_per_day <= 0:
        return np.empty(0, dtype=float)

    for day in range(first_day, last_day + 1):
        day_start = day * SECONDS_PER_DAY
        shift_open = day_start + capacity.shift_start_hour * SECONDS_PER_HOUR
        shift_len = capacity.shift_hours_per_day * SECONDS_PER_HOUR
        # Slot i sits at the midpoint of its share of the shift, so no review
        # lands exactly on the shift boundary.
        offsets = (np.arange(n_per_day) + 0.5) * (shift_len / n_per_day)
        slots.append(shift_open + offsets)

    if not slots:
        return np.empty(0, dtype=float)
    allslots = np.concatenate(slots)
    return np.sort(allslots[(allslots >= start_dt) & (allslots <= end_dt)])


def simulate(
    alerts: pd.DataFrame,
    policy: Policy,
    capacity: CapacityConfig,
    sla: SLAConfig,
    *,
    capacity_scale: float = 1.0,
    score_col: str = "score",
    arrival_col: str = "arrival_dt",
    drain_hours: float | None = None,
) -> SimulationResult:
    """Run one alert stream through one policy.

    ``alerts`` needs ``arrival_dt``, ``score``, ``amount`` and ``is_fraud``.
    Returns a copy with review outcomes attached; the input is not mutated.

    ``drain_hours`` extends review capacity past the final arrival, defaulting
    to the SLA deadline. Without it the operation is modelled as stopping work
    the instant the last alert lands, which has two bad consequences: an alert
    arriving near the end can never be reviewed no matter how it scores, and a
    stream whose arrivals all fall outside a shift window generates no review
    slots at all. Draining for one SLA period means every alert gets the review
    window its SLA actually promises, which is what makes the breach rate
    meaningful.
    """
    required = {arrival_col, score_col, "amount", "is_fraud"}
    missing = required.difference(alerts.columns)
    if missing:
        raise ValueError(f"alerts is missing required column(s): {sorted(missing)}")
    if len(alerts) == 0:
        raise ValueError("Cannot simulate an empty alert stream.")

    out = alerts.copy().reset_index(drop=True)
    out = out.sort_values(arrival_col).reset_index(drop=True)

    arrivals = out[arrival_col].to_numpy(dtype=float)
    scores = out[score_col].to_numpy(dtype=float)

    start_dt = float(arrivals.min())
    last_arrival = float(arrivals.max())
    drain = sla.deadline_hours if drain_hours is None else drain_hours
    end_dt = last_arrival + drain * SECONDS_PER_HOUR
    slots = generate_review_slots(start_dt, end_dt, capacity, capacity_scale)

    escalate_after = sla.escalate_after_hours * SECONDS_PER_HOUR
    share = float(np.clip(sla.escalation_capacity_share, 0.0, 1.0))

    n_alerts = len(out)
    reviewed_at = np.full(n_alerts, np.nan)
    review_order = np.full(n_alerts, -1, dtype=np.int64)
    via_escalation = np.zeros(n_alerts, dtype=bool)
    done = np.zeros(n_alerts, dtype=bool)

    # Pending alerts. Both heaps are min-heaps, so risk ranking negates the
    # score. An alert can sit in both heaps at once -- it is pushed to the
    # escalated tier when it ages without being removed from the risk heap --
    # so ``done`` is checked on pop and stale entries are discarded. Lazy
    # deletion keeps the whole run O(n log n); rebuilding the heap each slot to
    # keep it exact was O(n) per slot and dominated the runtime.
    normal: list[tuple] = []
    escalated: list[tuple] = []

    next_arrival = 0
    escalation_ptr = 0
    pending = 0
    escalation_credit = 0.0

    trace_t: list[float] = []
    trace_depth: list[int] = []
    order_counter = 0

    for slot_time in slots:
        # Admit everything that has arrived by now.
        while next_arrival < n_alerts and arrivals[next_arrival] <= slot_time:
            i = next_arrival
            if policy is Policy.FIFO:
                heapq.heappush(normal, (arrivals[i], i))
            else:
                heapq.heappush(normal, (-scores[i], arrivals[i], i))
            next_arrival += 1
            pending += 1

        # Promote anything that has aged past the escalation point. Arrivals are
        # sorted, so a single advancing pointer finds them all. Pure risk
        # ranking has no escalation tier and starves its tail on purpose.
        if policy is Policy.RISK_RANKED_AGING:
            cutoff = slot_time - escalate_after
            while escalation_ptr < next_arrival and arrivals[escalation_ptr] <= cutoff:
                if not done[escalation_ptr]:
                    heapq.heappush(escalated, (arrivals[escalation_ptr], escalation_ptr))
                escalation_ptr += 1

        idx = -1
        # Spend a reserved share of slots on the ageing tier. Accumulating
        # credit rather than testing slot parity keeps the realised share equal
        # to the configured one for any share, not just simple fractions.
        #
        # The credit is use-it-or-lose-it: while nothing has aged it is capped
        # at one slot's worth. Letting it bank while the tier sat empty meant a
        # quiet stretch could save up a hundred-odd slots, and the moment
        # alerts started ageing the tier took every pick until the bank was
        # spent -- a long burst of FIFO in the middle of risk ranking. Capping
        # keeps the first aged alert served promptly, but only that one.
        escalation_credit += share
        if not escalated:
            escalation_credit = min(escalation_credit, 1.0)
        elif escalation_credit >= 1.0:
            while escalated:
                _, cand = heapq.heappop(escalated)
                if not done[cand]:
                    idx = cand
                    via_escalation[cand] = True
                    escalation_credit -= 1.0
                    break

        if idx < 0:
            while normal:
                cand = heapq.heappop(normal)[-1]
                if not done[cand]:
                    idx = cand
                    break

        if idx < 0:
            trace_t.append(slot_time)
            trace_depth.append(0)
            continue

        done[idx] = True
        pending -= 1
        reviewed_at[idx] = slot_time
        review_order[idx] = order_counter
        order_counter += 1

        trace_t.append(slot_time)
        trace_depth.append(pending)

    out["reviewed"] = ~np.isnan(reviewed_at)
    out["reviewed_at"] = reviewed_at
    out["review_order"] = review_order
    # Which tier served the review. Only the aging policy ever sets it; it is
    # what lets a test check the realised escalation pattern rather than infer
    # it from wait times, which cannot tell an aged alert popped from the risk
    # heap apart from one served by the escalated tier.
    out["reviewed_via_escalation"] = via_escalation
    out["wait_seconds"] = reviewed_at - arrivals
    out["wait_hours"] = out["wait_seconds"] / SECONDS_PER_HOUR

    # An alert never reviewed has breached its SLA if the window ended after its
    # deadline; one reviewed late has breached it outright.
    deadline_s = sla.deadline_hours * SECONDS_PER_HOUR
    breached_late = out["reviewed"] & (out["wait_seconds"] > deadline_s)
    never = ~out["reviewed"]
    aged_out = never & ((end_dt - arrivals) > deadline_s)
    out["sla_breached"] = breached_late | aged_out

    # Caught fraud is fraud that was reviewed. Perfect review by assumption.
    out["fraud_caught"] = out["reviewed"] & (out["is_fraud"] == 1)
    out["fraud_missed"] = (~out["reviewed"]) & (out["is_fraud"] == 1)

    trace = pd.DataFrame({"t": trace_t, "queue_depth": trace_depth})
    if not trace.empty:
        trace["day"] = (trace["t"] - start_dt) / SECONDS_PER_DAY

    return SimulationResult(
        policy=policy,
        alerts=out,
        queue_trace=trace,
        capacity=capacity,
        sla=sla,
        review_slots=int(len(slots)),
        meta={
            "capacity_scale": capacity_scale,
            "arrival_window_days": (last_arrival - start_dt) / SECONDS_PER_DAY,
            "simulated_days_including_drain": (end_dt - start_dt) / SECONDS_PER_DAY,
            "drain_hours": drain,
            "start_dt": start_dt,
            "last_arrival_dt": last_arrival,
            "end_dt": end_dt,
        },
    )
