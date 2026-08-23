"""Outcome metrics for a simulated queue, and the policy comparison."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import SECONDS_PER_HOUR, CostConfig
from .simulator import SimulationResult


def summarise(result: SimulationResult, cost: CostConfig | None = None) -> dict:
    """Everything a review manager would ask about one simulated run."""
    cost = cost or CostConfig()
    a = result.alerts
    n = len(a)
    total_fraud = int((a["is_fraud"] == 1).sum())
    total_fraud_dollars = float(a.loc[a["is_fraud"] == 1, "amount"].sum())

    reviewed = a["reviewed"]
    n_reviewed = int(reviewed.sum())
    caught = a["fraud_caught"]
    n_caught = int(caught.sum())
    caught_dollars = float(a.loc[caught, "amount"].sum())
    missed_dollars = total_fraud_dollars - caught_dollars

    review_hours = n_reviewed * (cost.minutes_per_review / 60.0)
    fp_reviewed = int((reviewed & (a["is_fraud"] == 0)).sum())

    tt = a.loc[caught, "wait_hours"].dropna()
    # Wait across every reviewed alert, not only the fraud ones. The starvation
    # a risk-ranked queue creates lands on low-scoring alerts, which are almost
    # all legitimate -- so a fraud-only view of latency cannot see it at all.
    all_wait = a.loc[a["reviewed"], "wait_hours"].dropna()

    return {
        "policy": result.policy.value,
        "capacity_scale": result.meta.get("capacity_scale", 1.0),
        "n_alerts": n,
        "n_reviewed": n_reviewed,
        "review_completion_pct": 100.0 * n_reviewed / n if n else float("nan"),
        "review_hours": review_hours,
        "review_labour_cost": review_hours * cost.analyst_hourly_cost,
        "total_fraud_alerts": total_fraud,
        "fraud_caught": n_caught,
        "fraud_caught_pct": 100.0 * n_caught / total_fraud if total_fraud else float("nan"),
        "fraud_dollars_total": total_fraud_dollars,
        "fraud_dollars_caught": caught_dollars,
        "fraud_dollars_missed": missed_dollars,
        "fraud_dollars_caught_pct": (
            100.0 * caught_dollars / total_fraud_dollars if total_fraud_dollars else float("nan")
        ),
        "false_positives_reviewed": fp_reviewed,
        "precision_of_reviewed": n_caught / n_reviewed if n_reviewed else float("nan"),
        "median_hours_to_detection": float(tt.median()) if len(tt) else float("nan"),
        "p90_hours_to_detection": float(tt.quantile(0.90)) if len(tt) else float("nan"),
        "p99_hours_to_detection": float(tt.quantile(0.99)) if len(tt) else float("nan"),
        "mean_hours_to_detection": float(tt.mean()) if len(tt) else float("nan"),
        "p50_wait_any_alert": float(all_wait.median()) if len(all_wait) else float("nan"),
        "p99_wait_any_alert": float(all_wait.quantile(0.99)) if len(all_wait) else float("nan"),
        "max_wait_any_alert": float(all_wait.max()) if len(all_wait) else float("nan"),
        "sla_breach_pct": 100.0 * float(a["sla_breached"].mean()),
        "sla_breaches": int(a["sla_breached"].sum()),
        "peak_queue_depth": (
            int(result.queue_trace["queue_depth"].max()) if len(result.queue_trace) else 0
        ),
        "fraud_dollars_per_review_hour": (
            caught_dollars / review_hours if review_hours else float("nan")
        ),
    }


def compare_policies(
    results: dict[str, SimulationResult], cost: CostConfig | None = None
) -> pd.DataFrame:
    """One row per policy, in the order given."""
    return pd.DataFrame([summarise(r, cost) for r in results.values()])


def time_to_detection_distribution(
    result: SimulationResult, quantiles: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9, 0.99)
) -> pd.DataFrame:
    """Quantiles of the wait between a fraud alert arriving and being reviewed."""
    tt = result.alerts.loc[result.alerts["fraud_caught"], "wait_hours"].dropna()
    if tt.empty:
        return pd.DataFrame(columns=["quantile", "hours"])
    return pd.DataFrame(
        {"quantile": list(quantiles), "hours": [float(tt.quantile(q)) for q in quantiles]}
    )


def capacity_sweep(
    alerts: pd.DataFrame,
    policies: dict[str, "object"],
    capacity,
    sla,
    scales: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.85, 1.0, 1.25, 1.5),
    cost: CostConfig | None = None,
    score_col: str = "score",
) -> pd.DataFrame:
    """Fraud caught against analyst hours, for each policy across staffing levels.

    This is the automation argument in one table. Holding the alert stream fixed
    and varying only capacity shows how far review volume can be cut before
    detection degrades -- and how much further it can be cut when the queue is
    prioritised rather than worked in arrival order.
    """
    from .simulator import simulate  # local import to avoid a cycle

    rows = []
    for name, policy in policies.items():
        for s in scales:
            res = simulate(alerts, policy, capacity, sla,
                           capacity_scale=s, score_col=score_col)
            row = summarise(res, cost)
            row["policy_name"] = name
            rows.append(row)
    return pd.DataFrame(rows)


def policy_capacity_equivalence(
    sweep: pd.DataFrame,
    baseline: str = "fifo",
    challenger: str = "risk_ranked",
    baseline_scale: float = 1.0,
    metric: str = "fraud_dollars_caught_pct",
) -> dict:
    """How much capacity the challenger needs to match the baseline's capture.

    This is the automation argument stated as one number: run the baseline
    process at full staffing, then ask what staffing the prioritised process
    needs to catch the same fraud value. The gap is the headcount that
    prioritisation frees, and unlike a within-policy headroom figure it does not
    saturate when the queue is nearly clearing.

    Capacity is interpolated linearly between swept points, so the answer is a
    continuous estimate rather than the nearest grid value.
    """
    base = sweep[sweep["policy_name"] == baseline].sort_values("capacity_scale")
    chal = sweep[sweep["policy_name"] == challenger].sort_values("capacity_scale")
    if base.empty or chal.empty:
        return {}

    at = base[base["capacity_scale"] == baseline_scale]
    if at.empty:
        return {}
    target = float(at[metric].iloc[0])
    base_hours = float(at["review_hours"].iloc[0])

    reaching = chal[chal[metric] >= target]
    if reaching.empty:
        return {
            "baseline": baseline,
            "challenger": challenger,
            "target_metric": metric,
            "target_value": target,
            "challenger_can_match": False,
        }

    hi = reaching.iloc[0]
    below = chal[chal["capacity_scale"] < hi["capacity_scale"]]
    if below.empty:
        scale = float(hi["capacity_scale"])
        hours = float(hi["review_hours"])
    else:
        lo = below.iloc[-1]
        span = float(hi[metric]) - float(lo[metric])
        frac = 0.0 if span <= 0 else (target - float(lo[metric])) / span
        scale = float(lo["capacity_scale"]) + frac * (
            float(hi["capacity_scale"]) - float(lo["capacity_scale"])
        )
        hours = float(lo["review_hours"]) + frac * (
            float(hi["review_hours"]) - float(lo["review_hours"])
        )

    return {
        "baseline": baseline,
        "challenger": challenger,
        "target_metric": metric,
        "target_value": target,
        "challenger_can_match": True,
        "baseline_capacity_scale": baseline_scale,
        "baseline_review_hours": base_hours,
        "challenger_capacity_scale": scale,
        "challenger_review_hours": hours,
        "review_hours_saved": base_hours - hours,
        "capacity_reduction_pct": 100.0 * (1.0 - scale / baseline_scale),
    }


def capacity_headroom(
    sweep: pd.DataFrame, policy_name: str, degradation_pct: float = 5.0
) -> dict:
    """How far capacity can be cut before fraud-dollar capture drops materially.

    Defined against each policy's own full-capacity result, so the answer is
    "how much of *this* operation could be automated away", not a comparison
    against some other policy's ceiling.
    """
    sub = sweep[sweep["policy_name"] == policy_name].sort_values("capacity_scale")
    if sub.empty:
        return {}
    full = sub[sub["capacity_scale"] == 1.0]
    if full.empty:
        full = sub.iloc[[-1]]
    baseline = float(full["fraud_dollars_caught_pct"].iloc[0])
    floor = baseline - degradation_pct

    ok = sub[sub["fraud_dollars_caught_pct"] >= floor]
    if ok.empty:
        return {
            "policy": policy_name,
            "baseline_fraud_dollars_caught_pct": baseline,
            "degradation_allowance_pct": degradation_pct,
            "min_capacity_scale": float("nan"),
        }
    best = ok.loc[ok["capacity_scale"].idxmin()]
    return {
        "policy": policy_name,
        "baseline_fraud_dollars_caught_pct": baseline,
        "degradation_allowance_pct": degradation_pct,
        "min_capacity_scale": float(best["capacity_scale"]),
        "capacity_reduction_pct": 100.0 * (1.0 - float(best["capacity_scale"])),
        "review_hours_at_min": float(best["review_hours"]),
        "review_hours_at_full": float(full["review_hours"].iloc[0]),
        "review_hours_saved": float(full["review_hours"].iloc[0]) - float(best["review_hours"]),
        "fraud_dollars_caught_pct_at_min": float(best["fraud_dollars_caught_pct"]),
    }
