"""Outcome metrics, capacity analysis, and auto-disposition."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from triage.autodisposition import (
    apply_auto_disposition,
    auto_disposition_curve,
    best_auto_close_band,
)
from triage.config import CapacityConfig, SLAConfig
from triage.data import generate_synthetic_stream, select_alerts
from triage.metrics import (
    capacity_headroom,
    capacity_sweep,
    compare_policies,
    policy_capacity_equivalence,
    summarise,
    time_to_detection_distribution,
)
from triage.simulator import Policy, simulate

POLICIES = {
    "fifo": Policy.FIFO,
    "risk_ranked": Policy.RISK_RANKED,
    "risk_ranked_aging": Policy.RISK_RANKED_AGING,
}


# ------------------------------------------------------------------ summary


def test_review_hours_follow_reviews_and_handling_time(synthetic_alerts, sla, cost):
    res = simulate(synthetic_alerts, Policy.FIFO, CapacityConfig(), sla)
    s = summarise(res, cost)
    assert s["review_hours"] == pytest.approx(
        s["n_reviewed"] * cost.minutes_per_review / 60.0
    )


def test_caught_and_missed_fraud_dollars_sum_to_the_total(synthetic_alerts, sla, cost):
    res = simulate(synthetic_alerts, Policy.RISK_RANKED, CapacityConfig(n_analysts=1),
                   sla, capacity_scale=0.3)
    s = summarise(res, cost)
    assert s["fraud_dollars_caught"] + s["fraud_dollars_missed"] == pytest.approx(
        s["fraud_dollars_total"]
    )


def test_reviewed_alerts_split_into_fraud_and_false_positives(synthetic_alerts, sla, cost):
    res = simulate(synthetic_alerts, Policy.FIFO, CapacityConfig(n_analysts=1),
                   sla, capacity_scale=0.4)
    s = summarise(res, cost)
    assert s["fraud_caught"] + s["false_positives_reviewed"] == s["n_reviewed"]


def test_capture_percentages_stay_within_bounds(synthetic_alerts, sla, cost):
    for scale in (0.1, 0.5, 1.0):
        res = simulate(synthetic_alerts, Policy.RISK_RANKED, CapacityConfig(),
                       sla, capacity_scale=scale)
        s = summarise(res, cost)
        # Tolerance because these are ratios of float sums; catching every
        # fraud dollar lands on 100.00000000000001.
        assert 0.0 <= s["fraud_caught_pct"] <= 100.0 + 1e-9
        assert 0.0 <= s["fraud_dollars_caught_pct"] <= 100.0 + 1e-9
        assert 0.0 <= s["sla_breach_pct"] <= 100.0 + 1e-9


def test_detection_latency_quantiles_are_ordered(synthetic_alerts, sla, cost):
    res = simulate(synthetic_alerts, Policy.RISK_RANKED, CapacityConfig(n_analysts=1), sla)
    s = summarise(res, cost)
    assert s["median_hours_to_detection"] <= s["p90_hours_to_detection"]
    assert s["p90_hours_to_detection"] <= s["p99_hours_to_detection"]
    assert s["p99_wait_any_alert"] <= s["max_wait_any_alert"]


def test_time_to_detection_distribution_is_monotone(synthetic_alerts, sla):
    res = simulate(synthetic_alerts, Policy.FIFO, CapacityConfig(), sla)
    d = time_to_detection_distribution(res)
    assert (d["hours"].diff().dropna() >= -1e-9).all()


def test_compare_policies_returns_one_row_per_policy(synthetic_alerts, sla, cost):
    runs = {n: simulate(synthetic_alerts, p, CapacityConfig(), sla)
            for n, p in POLICIES.items()}
    df = compare_policies(runs, cost)
    assert len(df) == 3
    assert set(df["policy"]) == {p.value for p in POLICIES.values()}


# ----------------------------------------------------------------- capacity


def test_capacity_sweep_covers_every_policy_and_scale(synthetic_alerts, sla, cost):
    scales = (0.25, 0.5, 1.0)
    sweep = capacity_sweep(synthetic_alerts, POLICIES, CapacityConfig(), sla,
                           scales=scales, cost=cost)
    assert len(sweep) == len(POLICIES) * len(scales)


def test_more_capacity_never_catches_less_fraud(synthetic_alerts, sla, cost):
    sweep = capacity_sweep(synthetic_alerts, POLICIES, CapacityConfig(), sla,
                           scales=(0.2, 0.4, 0.6, 0.8, 1.0), cost=cost)
    for _, grp in sweep.groupby("policy_name"):
        g = grp.sort_values("capacity_scale")
        assert (g["fraud_dollars_caught"].diff().dropna() >= -1e-6).all()


def test_review_hours_rise_with_capacity_until_the_queue_clears(synthetic_alerts, sla, cost):
    """Hours rise with staffing, then flatten: once every alert is reviewed,
    extra capacity has nothing left to do."""
    sweep = capacity_sweep(synthetic_alerts, {"fifo": Policy.FIFO}, CapacityConfig(),
                           sla, scales=(0.1, 0.25, 0.5, 1.0), cost=cost)
    g = sweep.sort_values("capacity_scale")
    assert (g["review_hours"].diff().dropna() >= -1e-9).all()
    constrained = g[g["review_completion_pct"] < 99.9]
    assert (constrained["review_hours"].diff().dropna() > 0).all()


def test_capacity_equivalence_finds_a_cheaper_matching_point(synthetic_alerts, sla, cost):
    """The automation claim: risk ranking matches FIFO's capture on less capacity."""
    sweep = capacity_sweep(synthetic_alerts, POLICIES, CapacityConfig(), sla,
                           scales=(0.1, 0.2, 0.3, 0.5, 0.7, 1.0), cost=cost)
    eq = policy_capacity_equivalence(sweep, "fifo", "risk_ranked", baseline_scale=0.5)
    assert eq["challenger_can_match"]
    assert eq["challenger_review_hours"] < eq["baseline_review_hours"]
    assert eq["capacity_reduction_pct"] > 0


def test_capacity_equivalence_against_itself_saves_nothing(synthetic_alerts, sla, cost):
    """A policy cannot beat itself; the sanity check that the interpolation is
    not manufacturing savings."""
    sweep = capacity_sweep(synthetic_alerts, POLICIES, CapacityConfig(), sla,
                           scales=(0.2, 0.5, 1.0), cost=cost)
    eq = policy_capacity_equivalence(sweep, "fifo", "fifo", baseline_scale=0.5)
    assert eq["capacity_reduction_pct"] == pytest.approx(0.0, abs=1e-6)


def test_capacity_headroom_reports_a_scale_at_or_below_full(synthetic_alerts, sla, cost):
    sweep = capacity_sweep(synthetic_alerts, POLICIES, CapacityConfig(), sla,
                           scales=(0.3, 0.5, 0.7, 1.0), cost=cost)
    h = capacity_headroom(sweep, "risk_ranked", degradation_pct=5.0)
    assert h["min_capacity_scale"] <= 1.0
    assert h["fraud_dollars_caught_pct_at_min"] >= (
        h["baseline_fraud_dollars_caught_pct"] - 5.0 - 1e-9
    )


def test_capacity_headroom_of_an_unknown_policy_is_empty(synthetic_alerts, sla, cost):
    sweep = capacity_sweep(synthetic_alerts, {"fifo": Policy.FIFO}, CapacityConfig(),
                           sla, scales=(0.5, 1.0), cost=cost)
    assert capacity_headroom(sweep, "does_not_exist") == {}


# -------------------------------------------------------- auto-disposition


def test_auto_disposition_splits_the_stream_at_the_band(synthetic_alerts):
    kept, closed = apply_auto_disposition(synthetic_alerts, 0.2)
    assert len(kept) + len(closed) == len(synthetic_alerts)
    assert (kept["score"] >= 0.2).all()
    assert (closed["score"] < 0.2).all()


def test_a_zero_band_closes_nothing(synthetic_alerts):
    kept, closed = apply_auto_disposition(synthetic_alerts, 0.0)
    assert len(closed) == 0
    assert len(kept) == len(synthetic_alerts)


def test_raising_the_band_closes_more_alerts(synthetic_alerts, sla, cost):
    curve = auto_disposition_curve(synthetic_alerts, CapacityConfig(), sla,
                                   thresholds=(0.05, 0.1, 0.2, 0.4), cost=cost)
    assert (curve["n_auto_closed"].diff().dropna() >= 0).all()
    assert (curve["auto_closed_pct"].diff().dropna() >= 0).all()


def test_raising_the_band_frees_review_hours(synthetic_alerts, sla, cost):
    curve = auto_disposition_curve(synthetic_alerts, CapacityConfig(), sla,
                                   thresholds=(0.05, 0.2, 0.5), cost=cost)
    assert (curve["review_hours"].diff().dropna() <= 1e-9).all()


def test_auto_closed_fraud_counts_against_capture(synthetic_alerts, sla, cost):
    """Fraud closed without review is missed fraud, not fraud that stopped existing.

    The denominator stays the full stream, so a band cannot flatter itself by
    discarding the alerts it fails on.
    """
    curve = auto_disposition_curve(synthetic_alerts, CapacityConfig(), sla,
                                   thresholds=(0.05, 0.9), cost=cost)
    aggressive = curve.iloc[-1]
    assert aggressive["auto_closed_fraud"] > 0
    assert aggressive["fraud_dollars_caught_pct"] < 100.0


def test_band_selection_respects_the_loss_tolerance(synthetic_alerts, sla, cost):
    curve = auto_disposition_curve(synthetic_alerts, CapacityConfig(), sla,
                                   thresholds=(0.05, 0.1, 0.15, 0.2, 0.3), cost=cost)
    band = best_auto_close_band(curve, max_dollar_loss_pct=2.0)
    if band.get("feasible"):
        assert band["fraud_dollars_caught_pct"] >= (
            band["baseline_fraud_dollars_caught_pct"] - 2.0 - 1e-9
        )


def test_band_selection_on_an_empty_curve_returns_empty():
    assert best_auto_close_band(pd.DataFrame()) == {}


# ---------------------------------------------------------------- the data


def test_synthetic_stream_has_the_exported_schema():
    s = generate_synthetic_stream(n=500)
    for col in ("transaction_id", "transaction_dt", "amount", "is_fraud",
                "score_raw", "signal_1_name", "signal_1_contribution"):
        assert col in s.columns


def test_synthetic_stream_is_reproducible():
    a = generate_synthetic_stream(n=300, seed=42)
    b = generate_synthetic_stream(n=300, seed=42)
    pd.testing.assert_frame_equal(a, b)


def test_synthetic_stream_arrives_in_time_order():
    s = generate_synthetic_stream(n=1000)
    assert (s["arrival_dt"].diff().dropna() >= 0).all()


def test_synthetic_fraud_scores_higher_than_legitimate_on_average():
    s = generate_synthetic_stream(n=20_000)
    assert s.loc[s["is_fraud"] == 1, "score_raw"].mean() > \
        s.loc[s["is_fraud"] == 0, "score_raw"].mean()


def test_select_alerts_applies_the_threshold():
    s = generate_synthetic_stream(n=2000)
    a = select_alerts(s, 0.1)
    assert (a["score"] >= 0.1).all()
    assert len(a) == int((s["score"] >= 0.1).sum())
