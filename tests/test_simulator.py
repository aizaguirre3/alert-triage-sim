"""Queue simulator correctness.

The policy comparison is the deliverable, so the ordering rules and the capacity
accounting are what these tests pin down. Where possible the expected review
sequence is worked out by hand rather than read back from the implementation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from triage.config import CapacityConfig, SLAConfig
from triage.simulator import Policy, generate_review_slots, simulate

HOUR = 3600.0
DAY = 86400.0


# ------------------------------------------------------------ capacity slots


def test_slot_count_matches_configured_throughput(tiny_capacity):
    """1 review/hour x 8 shift hours = 8 slots per day.

    The window [day 10 00:00, day 11 00:00) spans one shift, day 10's; day 11's
    shift opens at 08:00 on day 11, past the end of the window.
    """
    slots = generate_review_slots(10 * DAY, 11 * DAY, tiny_capacity)
    assert len(slots) == 8
    assert len(generate_review_slots(10 * DAY, 11.75 * DAY, tiny_capacity)) == 16


def test_slots_fall_inside_the_shift_window(tiny_capacity):
    slots = generate_review_slots(10 * DAY, 12 * DAY, tiny_capacity)
    hours = (slots % DAY) / HOUR
    assert hours.min() >= tiny_capacity.shift_start_hour
    assert hours.max() < tiny_capacity.shift_end_hour


def test_slots_are_sorted(tiny_capacity):
    slots = generate_review_slots(10 * DAY, 15 * DAY, tiny_capacity)
    assert (np.diff(slots) >= 0).all()


def test_capacity_scale_multiplies_throughput(tiny_capacity):
    full = generate_review_slots(10 * DAY, 12 * DAY, tiny_capacity, 1.0)
    half = generate_review_slots(10 * DAY, 12 * DAY, tiny_capacity, 0.5)
    assert len(half) == pytest.approx(len(full) / 2, abs=2)


def test_zero_capacity_produces_no_slots(tiny_capacity):
    assert len(generate_review_slots(10 * DAY, 12 * DAY, tiny_capacity, 0.0)) == 0


def test_negative_capacity_scale_is_rejected(tiny_capacity):
    with pytest.raises(ValueError, match="non-negative"):
        generate_review_slots(10 * DAY, 12 * DAY, tiny_capacity, -1.0)


def test_more_analysts_means_more_slots():
    one = generate_review_slots(10 * DAY, 11 * DAY, CapacityConfig(n_analysts=1))
    three = generate_review_slots(10 * DAY, 11 * DAY, CapacityConfig(n_analysts=3))
    assert len(three) == 3 * len(one)


# ------------------------------------------------------------------ ordering


def test_fifo_reviews_in_arrival_order(tiny_alerts, tiny_capacity, sla):
    res = simulate(tiny_alerts, Policy.FIFO, tiny_capacity, sla)
    got = res.alerts.sort_values("review_order")["transaction_id"].tolist()
    assert got == [1, 2, 3, 4, 5, 6]


def test_risk_ranked_reviews_in_descending_score_order(tiny_alerts, tiny_capacity, sla):
    res = simulate(tiny_alerts, Policy.RISK_RANKED, tiny_capacity, sla)
    got = res.alerts.sort_values("review_order")["transaction_id"].tolist()
    # scores 0.99, 0.90, 0.70, 0.50, 0.30, 0.10 -> ids 6, 2, 4, 5, 3, 1
    assert got == [6, 2, 4, 5, 3, 1]


def test_risk_ranked_reviews_the_highest_scoring_alert_first(synthetic_alerts, sla):
    cap = CapacityConfig(n_analysts=1, minutes_per_review=60.0)
    res = simulate(synthetic_alerts, Policy.RISK_RANKED, cap, sla)
    first = res.alerts.loc[res.alerts["review_order"] == 0]
    arrived_first_day = res.alerts[
        res.alerts["arrival_dt"] <= float(first["arrival_dt"].iloc[0])
    ]
    assert first["score"].iloc[0] == arrived_first_day["score"].max()


def test_an_alert_cannot_be_reviewed_before_it_arrives(synthetic_alerts, sla):
    cap = CapacityConfig(n_analysts=2)
    for policy in Policy:
        res = simulate(synthetic_alerts, policy, cap, sla)
        r = res.reviewed
        assert (r["reviewed_at"] >= r["arrival_dt"]).all(), policy


def test_every_alert_is_reviewed_at_most_once(synthetic_alerts, sla):
    """Lazy deletion puts an aged alert in two heaps at once; it must still be
    picked exactly once."""
    res = simulate(synthetic_alerts, Policy.RISK_RANKED_AGING, CapacityConfig(), sla)
    orders = res.alerts.loc[res.alerts["reviewed"], "review_order"]
    assert orders.is_unique
    assert sorted(orders) == list(range(len(orders)))


def test_review_count_never_exceeds_available_slots(synthetic_alerts, sla):
    cap = CapacityConfig(n_analysts=1)
    for policy in Policy:
        res = simulate(synthetic_alerts, policy, cap, sla)
        assert int(res.alerts["reviewed"].sum()) <= res.review_slots, policy


def test_all_policies_review_the_same_number_of_alerts(synthetic_alerts, sla):
    """Policy changes which alerts get reviewed, never how many. Any difference
    would confound every comparison built on top."""
    cap = CapacityConfig()
    counts = {
        p: int(simulate(synthetic_alerts, p, cap, sla).alerts["reviewed"].sum())
        for p in Policy
    }
    assert len(set(counts.values())) == 1, counts


def test_risk_ranking_catches_more_fraud_than_fifo_under_constraint(synthetic_alerts, sla):
    """Ordering can only matter when capacity binds. At 20% capacity the queue
    cannot clear, so which alerts get picked decides how much fraud is caught."""
    cap = CapacityConfig(n_analysts=1)
    fifo = simulate(synthetic_alerts, Policy.FIFO, cap, sla, capacity_scale=0.2)
    risk = simulate(synthetic_alerts, Policy.RISK_RANKED, cap, sla, capacity_scale=0.2)
    assert int(fifo.alerts["reviewed"].sum()) < len(synthetic_alerts)  # genuinely constrained
    assert int(risk.alerts["fraud_caught"].sum()) > int(fifo.alerts["fraud_caught"].sum())


def test_policy_makes_no_difference_when_capacity_is_ample(synthetic_alerts, sla):
    """The converse, and a guard against overselling prioritisation: if every
    alert gets reviewed anyway, queue order changes nothing about what is
    caught."""
    cap = CapacityConfig(n_analysts=8)
    fifo = simulate(synthetic_alerts, Policy.FIFO, cap, sla)
    risk = simulate(synthetic_alerts, Policy.RISK_RANKED, cap, sla)
    assert bool(fifo.alerts["reviewed"].all())
    assert int(risk.alerts["fraud_caught"].sum()) == int(fifo.alerts["fraud_caught"].sum())


# -------------------------------------------------------------------- aging


def test_aging_bounds_the_worst_case_wait(synthetic_alerts, sla):
    """Pure risk ranking starves its tail; escalation is what stops that.

    This is the property aging exists for, and it is not the same as a lower SLA
    breach rate -- see the README, where aging bounds the tail while breaching
    more often.
    """
    cap = CapacityConfig(n_analysts=1)
    risk = simulate(synthetic_alerts, Policy.RISK_RANKED, cap, sla)
    aging = simulate(synthetic_alerts, Policy.RISK_RANKED_AGING, cap, sla)
    assert aging.reviewed["wait_hours"].max() < risk.reviewed["wait_hours"].max()


def test_a_full_escalation_share_degenerates_into_fifo(tiny_alerts, tiny_capacity):
    """The trap that motivated the reserved-share design.

    With every slot serving the escalated tier on a backlogged queue, everything
    ages past the escalation point and gets worked in arrival order.
    """
    aggressive = SLAConfig(escalate_after_hours=0.0, escalation_capacity_share=1.0)
    aged = simulate(tiny_alerts, Policy.RISK_RANKED_AGING, tiny_capacity, aggressive)
    fifo = simulate(tiny_alerts, Policy.FIFO, tiny_capacity, aggressive)
    assert (
        aged.alerts.sort_values("review_order")["transaction_id"].tolist()
        == fifo.alerts.sort_values("review_order")["transaction_id"].tolist()
    )


def test_a_zero_escalation_share_is_pure_risk_ranking(tiny_alerts, tiny_capacity):
    never = SLAConfig(escalate_after_hours=0.0, escalation_capacity_share=0.0)
    aged = simulate(tiny_alerts, Policy.RISK_RANKED_AGING, tiny_capacity, never)
    risk = simulate(tiny_alerts, Policy.RISK_RANKED, tiny_capacity, never)
    assert (
        aged.alerts.sort_values("review_order")["transaction_id"].tolist()
        == risk.alerts.sort_values("review_order")["transaction_id"].tolist()
    )


def test_escalation_share_interpolates_between_risk_ranking_and_fifo(synthetic_alerts):
    """The reserved share is a dial between the two pure policies.

    Fraud caught is the statistic to assert on: the median reviewed score is too
    insensitive to register a one-third diversion, while fraud caught moves
    monotonically across the whole range.
    """
    cap = CapacityConfig(n_analysts=1)
    kw = {"capacity_scale": 0.2}  # constrained, or every policy reviews everything

    def caught(share: float) -> int:
        sla = SLAConfig(escalate_after_hours=1.0, escalation_capacity_share=share)
        res = simulate(synthetic_alerts, Policy.RISK_RANKED_AGING, cap, sla, **kw)
        return int(res.alerts["fraud_caught"].sum())

    base_sla = SLAConfig(escalate_after_hours=1.0)
    fifo = int(simulate(synthetic_alerts, Policy.FIFO, cap, base_sla, **kw)
               .alerts["fraud_caught"].sum())
    risk = int(simulate(synthetic_alerts, Policy.RISK_RANKED, cap, base_sla, **kw)
               .alerts["fraud_caught"].sum())

    # The endpoints are the pure policies exactly, and the interior is monotone.
    assert caught(0.0) == risk
    assert caught(1.0) == fifo
    assert fifo < caught(0.5) < caught(1 / 3) <= risk


# ------------------------------------------------------------ wait times, SLA


def test_wait_hours_is_review_time_minus_arrival(synthetic_alerts, sla):
    res = simulate(synthetic_alerts, Policy.FIFO, CapacityConfig(), sla)
    r = res.reviewed
    np.testing.assert_allclose(
        r["wait_hours"], (r["reviewed_at"] - r["arrival_dt"]) / HOUR, rtol=1e-9
    )


def test_unreviewed_alerts_have_no_wait_time(synthetic_alerts, sla):
    res = simulate(synthetic_alerts, Policy.FIFO, CapacityConfig(n_analysts=1), sla)
    assert res.unreviewed["wait_hours"].isna().all()


def test_an_alert_reviewed_after_the_deadline_breaches(tiny_capacity):
    alerts = pd.DataFrame(
        {
            "transaction_id": [1],
            "arrival_dt": [10 * DAY],           # midnight; first slot is 08:30
            "score": [0.5],
            "amount": [100.0],
            "is_fraud": [0],
        }
    )
    # Deadline 04:00, but the shift does not open until 08:00, so the review
    # lands at 08:30 -- late. drain_hours is set explicitly so the alert is
    # actually reviewed rather than merely ageing out unreviewed.
    res = simulate(alerts, Policy.FIFO, tiny_capacity, SLAConfig(deadline_hours=4.0),
                   drain_hours=24.0)
    assert bool(res.alerts["reviewed"].iloc[0])
    assert res.alerts["wait_hours"].iloc[0] > 4.0
    assert bool(res.alerts["sla_breached"].iloc[0])


def test_an_unreviewed_alert_past_its_deadline_breaches(tiny_capacity):
    """Never being looked at is a breach too, not an absence of data."""
    alerts = pd.DataFrame(
        {
            "transaction_id": [1],
            "arrival_dt": [10 * DAY],
            "score": [0.5],
            "amount": [100.0],
            "is_fraud": [0],
        }
    )
    res = simulate(alerts, Policy.FIFO, tiny_capacity, SLAConfig(deadline_hours=2.0),
                   capacity_scale=0.0, drain_hours=48.0)
    assert not bool(res.alerts["reviewed"].iloc[0])
    assert bool(res.alerts["sla_breached"].iloc[0])


def test_an_alert_reviewed_inside_the_deadline_does_not_breach(tiny_capacity):
    alerts = pd.DataFrame(
        {
            "transaction_id": [1],
            "arrival_dt": [10 * DAY],
            "score": [0.5],
            "amount": [100.0],
            "is_fraud": [0],
        }
    )
    res = simulate(alerts, Policy.FIFO, tiny_capacity, SLAConfig(deadline_hours=24.0))
    assert not bool(res.alerts["sla_breached"].iloc[0])


def test_fraud_caught_requires_the_alert_to_be_reviewed(synthetic_alerts, sla):
    res = simulate(synthetic_alerts, Policy.RISK_RANKED, CapacityConfig(n_analysts=1), sla)
    a = res.alerts
    assert (a.loc[a["fraud_caught"], "reviewed"]).all()
    assert not (a.loc[a["fraud_missed"], "reviewed"]).any()
    assert int((a["fraud_caught"] | a["fraud_missed"]).sum()) == int((a["is_fraud"] == 1).sum())


# ------------------------------------------------------------------ contract


def test_missing_columns_are_rejected(tiny_capacity, sla):
    bad = pd.DataFrame({"arrival_dt": [1.0], "score": [0.5]})
    with pytest.raises(ValueError, match="missing required column"):
        simulate(bad, Policy.FIFO, tiny_capacity, sla)


def test_an_empty_stream_is_rejected(tiny_capacity, sla):
    empty = pd.DataFrame(
        {"arrival_dt": [], "score": [], "amount": [], "is_fraud": []}
    )
    with pytest.raises(ValueError, match="empty alert stream"):
        simulate(empty, Policy.FIFO, tiny_capacity, sla)


def test_the_input_frame_is_not_mutated(tiny_alerts, tiny_capacity, sla):
    before = tiny_alerts.copy()
    simulate(tiny_alerts, Policy.FIFO, tiny_capacity, sla)
    pd.testing.assert_frame_equal(tiny_alerts, before)


def test_queue_depth_trace_is_never_negative(synthetic_alerts, sla):
    res = simulate(synthetic_alerts, Policy.RISK_RANKED_AGING, CapacityConfig(), sla)
    assert (res.queue_trace["queue_depth"] >= 0).all()


def test_escalation_credit_does_not_bank_while_the_tier_is_empty(synthetic_alerts, sla):
    """The reserved share is use-it-or-lose-it.

    Credit for the escalated tier used to accrue on every slot, including the
    long stretch at the start of a run before anything has aged. Once alerts
    began ageing the banked credit was spent in one go and the tier took every
    pick for dozens of consecutive slots -- a burst of FIFO inside risk
    ranking. On this fixture the old behaviour produced a run of 47. With a
    one-third share and no banking, two consecutive picks from the tier are
    impossible, and the realised share can never exceed the configured one by
    more than the single slot the cap allows.
    """
    assert sla.escalation_capacity_share == pytest.approx(1 / 3)
    cap = CapacityConfig(n_analysts=1)
    res = simulate(synthetic_alerts, Policy.RISK_RANKED_AGING, cap, sla)
    served = res.reviewed.sort_values("review_order")["reviewed_via_escalation"].to_numpy()

    assert served.sum() > 0  # the tier was actually exercised
    assert not (served[1:] & served[:-1]).any(), "two consecutive escalated-tier picks"
    assert served.sum() <= len(served) / 3 + 1


def test_only_the_aging_policy_serves_from_the_escalated_tier(synthetic_alerts, sla):
    cap = CapacityConfig(n_analysts=1)
    for policy in (Policy.FIFO, Policy.RISK_RANKED):
        res = simulate(synthetic_alerts, policy, cap, sla)
        assert not res.alerts["reviewed_via_escalation"].any(), policy
