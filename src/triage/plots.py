"""Figures for the queue simulation."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

FIGSIZE = (9.5, 5.5)
DPI = 140
COLORS = {"fifo": "#c0392b", "risk_ranked": "#2471a3", "risk_ranked_aging": "#1e8449"}
LABELS = {
    "fifo": "FIFO (manual baseline)",
    "risk_ranked": "Risk-ranked",
    "risk_ranked_aging": "Risk-ranked + aging escalation",
}


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_capacity_curve(sweep: pd.DataFrame, path: Path) -> Path:
    """Fraud dollars caught against analyst hours, per policy.

    The headline chart. If prioritisation is worth anything, its curve sits
    above FIFO's everywhere, and the horizontal gap at a fixed capture level is
    the analyst hours that prioritisation saves.
    """
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(13, 5.2))
    for name, grp in sweep.groupby("policy_name"):
        g = grp.sort_values("review_hours")
        c = COLORS.get(name)
        ax.plot(g["review_hours"], g["fraud_dollars_caught"], "o-", lw=2, ms=4,
                color=c, label=LABELS.get(name, name))
        ax2.plot(g["review_hours"], g["fraud_dollars_caught_pct"], "o-", lw=2, ms=4,
                 color=c, label=LABELS.get(name, name))
    ax.set_xlabel("Analyst review hours")
    ax.set_ylabel("Fraud dollars caught")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    ax.set_title("Fraud dollars recovered vs review capacity")

    ax2.set_xlabel("Analyst review hours")
    ax2.set_ylabel("% of fraud dollars caught")
    ax2.grid(alpha=0.3)
    ax2.legend(fontsize=9)
    ax2.set_title("Same, as a share of available fraud")
    fig.suptitle("Queue policy at varying review capacity", y=1.02)
    return _save(fig, path)


def plot_queue_depth(traces: dict[str, pd.DataFrame], path: Path) -> Path:
    """Backlog over time. Shows whether the operation keeps up."""
    fig, ax = plt.subplots(figsize=FIGSIZE)
    for name, t in traces.items():
        if t.empty:
            continue
        ax.plot(t["day"], t["queue_depth"], lw=1.2, color=COLORS.get(name),
                label=LABELS.get(name, name), alpha=0.85)
    ax.set_xlabel("Day of test window")
    ax.set_ylabel("Alerts waiting in queue")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    ax.set_title("Review backlog over time at full capacity")
    return _save(fig, path)


def plot_time_to_detection(dists: dict[str, pd.Series], path: Path) -> Path:
    """Distribution of the wait before a fraud alert is actually looked at."""
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(13, 5.0))
    for name, s in dists.items():
        if s.empty:
            continue
        ax.hist(s, bins=60, histtype="step", lw=1.8, color=COLORS.get(name),
                label=LABELS.get(name, name))
        srt = s.sort_values()
        ax2.plot(srt, (srt.rank(method="first") / len(srt)) * 100,
                 lw=2, color=COLORS.get(name), label=LABELS.get(name, name))
    ax.set_xlabel("Hours from alert to review")
    ax.set_ylabel("Confirmed fraud alerts")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    ax.set_title("Time to detection")

    ax2.set_xlabel("Hours from alert to review")
    ax2.set_ylabel("% of caught fraud detected by this time")
    ax2.grid(alpha=0.3)
    ax2.legend(fontsize=9)
    ax2.set_title("Cumulative detection")
    return _save(fig, path)


def plot_auto_disposition(curve: pd.DataFrame, path: Path, chosen: float | None = None) -> Path:
    """What an auto-close band costs and what it buys."""
    fig, ax = plt.subplots(figsize=FIGSIZE)
    ax.plot(curve["auto_closed_pct"], curve["fraud_dollars_caught_pct"], "o-", lw=2,
            color="#2471a3", label="% fraud dollars caught")
    ax.set_xlabel("% of alert volume auto-closed without human review")
    ax.set_ylabel("% of fraud dollars caught")
    ax.grid(alpha=0.3)

    ax2 = ax.twinx()
    ax2.plot(curve["auto_closed_pct"], curve["review_hours"], "s--", lw=1.6,
             color="#c0392b", alpha=0.8, label="Analyst hours required")
    ax2.set_ylabel("Analyst review hours")

    if chosen is not None:
        ax.axvline(chosen, color="#1e8449", ls=":", lw=2)
        ax.text(chosen, ax.get_ylim()[0], "  selected band", color="#1e8449",
                fontsize=9, va="bottom")

    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="center left", fontsize=9)
    ax.set_title("Auto-disposition tradeoff: automation against detection")
    return _save(fig, path)


def plot_sla_breach(comparison: pd.DataFrame, path: Path) -> Path:
    """SLA breach rate by policy."""
    fig, ax = plt.subplots(figsize=(8, 4.6))
    names = comparison["policy"].tolist()
    vals = comparison["sla_breach_pct"].tolist()
    ax.bar([LABELS.get(n, n) for n in names], vals,
           color=[COLORS.get(n, "grey") for n in names])
    for i, v in enumerate(vals):
        ax.text(i, v, f" {v:.1f}%", ha="center", va="bottom", fontsize=10)
    ax.set_ylabel("% of alerts breaching SLA")
    ax.grid(alpha=0.3, axis="y")
    ax.set_title("SLA breach rate by queue policy")
    plt.setp(ax.get_xticklabels(), rotation=12, ha="right")
    return _save(fig, path)
