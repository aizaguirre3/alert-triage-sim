"""Auto-disposition: closing low-risk alerts without human review.

This is the automation lever the role description is actually about. Every alert
below some score closes automatically, freeing analyst hours for the rest of the
queue. The question is what that band costs in missed fraud and buys in review
capacity, across the full range of where the band could sit.

Two effects, and the second is the one people forget:

1. **Direct** -- fraud inside the auto-closed band is never seen.
2. **Indirect** -- the hours freed get spent on the alerts that remain, so under
   a finite-capacity queue a well-placed band can *increase* total fraud caught
   even though it discards some alerts outright.

The tradeoff curve below is computed under simulation rather than arithmetic
precisely so the second effect is included.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import CapacityConfig, CostConfig, SLAConfig
from .metrics import summarise
from .simulator import Policy, simulate


def apply_auto_disposition(
    alerts: pd.DataFrame, auto_close_below: float, score_col: str = "score"
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split the stream into auto-closed and human-reviewed portions."""
    closed = alerts[alerts[score_col] < auto_close_below]
    kept = alerts[alerts[score_col] >= auto_close_below]
    return kept, closed


def auto_disposition_curve(
    alerts: pd.DataFrame,
    capacity: CapacityConfig,
    sla: SLAConfig,
    policy: Policy = Policy.RISK_RANKED_AGING,
    thresholds: tuple[float, ...] | np.ndarray | None = None,
    cost: CostConfig | None = None,
    score_col: str = "score",
) -> pd.DataFrame:
    """Simulate the queue at each auto-close threshold.

    Reported metrics count the auto-closed fraud as missed, because it is: no
    human ever looked at it.
    """
    cost = cost or CostConfig()
    if thresholds is None:
        qs = np.linspace(0.0, 0.95, 20)
        thresholds = np.unique(np.quantile(alerts[score_col], qs))

    total_fraud = int((alerts["is_fraud"] == 1).sum())
    total_fraud_dollars = float(alerts.loc[alerts["is_fraud"] == 1, "amount"].sum())

    rows = []
    for t in thresholds:
        kept, closed = apply_auto_disposition(alerts, float(t), score_col)
        auto_closed_fraud = int((closed["is_fraud"] == 1).sum())
        auto_closed_fraud_dollars = float(
            closed.loc[closed["is_fraud"] == 1, "amount"].sum()
        )

        if len(kept) == 0:
            rows.append(
                {
                    "auto_close_below": float(t),
                    "n_auto_closed": int(len(closed)),
                    "auto_closed_pct": 100.0,
                    "n_human_reviewed": 0,
                    "review_hours": 0.0,
                    "review_hours_saved_vs_no_auto_close": np.nan,
                    "auto_closed_fraud": auto_closed_fraud,
                    "auto_closed_fraud_dollars": auto_closed_fraud_dollars,
                    "fraud_caught": 0,
                    "fraud_dollars_caught": 0.0,
                    "fraud_dollars_caught_pct": 0.0,
                    "sla_breach_pct": 0.0,
                }
            )
            continue

        res = simulate(kept, policy, capacity, sla, score_col=score_col)
        s = summarise(res, cost)
        rows.append(
            {
                "auto_close_below": float(t),
                "n_auto_closed": int(len(closed)),
                "auto_closed_pct": 100.0 * len(closed) / len(alerts),
                "n_human_reviewed": s["n_reviewed"],
                "review_hours": s["review_hours"],
                "auto_closed_fraud": auto_closed_fraud,
                "auto_closed_fraud_dollars": auto_closed_fraud_dollars,
                "fraud_caught": s["fraud_caught"],
                # Denominators are the FULL stream, so auto-closed fraud counts
                # against the policy rather than vanishing from the metric.
                "fraud_caught_pct": (
                    100.0 * s["fraud_caught"] / total_fraud if total_fraud else np.nan
                ),
                "fraud_dollars_caught": s["fraud_dollars_caught"],
                "fraud_dollars_caught_pct": (
                    100.0 * s["fraud_dollars_caught"] / total_fraud_dollars
                    if total_fraud_dollars
                    else np.nan
                ),
                "sla_breach_pct": s["sla_breach_pct"],
                "median_hours_to_detection": s["median_hours_to_detection"],
            }
        )

    out = pd.DataFrame(rows)
    if not out.empty:
        base_hours = float(out.loc[out["auto_close_below"].idxmin(), "review_hours"])
        out["review_hours_saved_vs_no_auto_close"] = base_hours - out["review_hours"]
    return out


def best_auto_close_band(curve: pd.DataFrame, max_dollar_loss_pct: float = 2.0) -> dict:
    """The most aggressive band that stays within a tolerated capture loss.

    Framed as a constraint rather than an optimum on purpose: "how much can I
    automate without losing more than 2% of fraud dollars" is the question an
    operations lead actually asks, and it does not require pricing analyst time
    against fraud loss a second time.
    """
    if curve.empty:
        return {}
    baseline = float(curve.loc[curve["auto_close_below"].idxmin(), "fraud_dollars_caught_pct"])
    floor = baseline - max_dollar_loss_pct
    ok = curve[curve["fraud_dollars_caught_pct"] >= floor]
    if ok.empty:
        return {"baseline_fraud_dollars_caught_pct": baseline, "feasible": False}
    best = ok.loc[ok["auto_closed_pct"].idxmax()]
    return {
        "feasible": True,
        "baseline_fraud_dollars_caught_pct": baseline,
        "max_dollar_loss_pct": max_dollar_loss_pct,
        "auto_close_below": float(best["auto_close_below"]),
        "auto_closed_pct": float(best["auto_closed_pct"]),
        "n_auto_closed": int(best["n_auto_closed"]),
        "review_hours": float(best["review_hours"]),
        "review_hours_saved": float(best["review_hours_saved_vs_no_auto_close"]),
        "fraud_dollars_caught_pct": float(best["fraud_dollars_caught_pct"]),
        "auto_closed_fraud_dollars": float(best["auto_closed_fraud_dollars"]),
    }
