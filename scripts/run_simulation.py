"""End-to-end P2 run: policies, capacity sweep, auto-disposition, SLA.

Single entry point. Reproduces every number in the README.
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np
import pandas as pd

from triage import autodisposition as auto
from triage import config, metrics, plots
from triage.data import generate_synthetic_stream, load_alert_stream, select_alerts
from triage.simulator import Policy, simulate

POLICIES = {
    "fifo": Policy.FIFO,
    "risk_ranked": Policy.RISK_RANKED,
    "risk_ranked_aging": Policy.RISK_RANKED_AGING,
}


def _jsonable(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true",
                    help="Use the synthetic stream instead of the exported one. "
                         "Numbers from a synthetic run are NOT reportable.")
    ap.add_argument("--threshold", type=float, default=None,
                    help="Detection threshold. Defaults to the operating "
                         "threshold recorded in the stream metadata.")
    ap.add_argument("--score-col", default="score_raw")
    args = ap.parse_args()

    cfg = config.DEFAULT
    config.ensure_dirs()
    t0 = time.time()
    results: dict = {"synthetic": args.synthetic}

    # ------------------------------------------------------------- stream
    print("[1/5] Loading alert stream ...")
    if args.synthetic:
        stream = generate_synthetic_stream()
        meta = {"source": "synthetic", "operating_threshold": 0.05}
    else:
        stream, meta = load_alert_stream(score_col=args.score_col)
    results["stream_metadata"] = meta

    threshold = args.threshold
    if threshold is None:
        threshold = float(meta.get("operating_threshold", 0.05))
    alerts = select_alerts(stream, threshold)

    window_days = (
        float(alerts["arrival_dt"].max()) - float(alerts["arrival_dt"].min())
    ) / config.SECONDS_PER_DAY
    fraud_dollars = float(alerts.loc[alerts["is_fraud"] == 1, "amount"].sum())

    results["stream"] = {
        "scored_transactions": int(len(stream)),
        "detection_threshold": threshold,
        "alerts": int(len(alerts)),
        "alert_rate_pct": 100.0 * len(alerts) / len(stream),
        "fraud_alerts": int((alerts["is_fraud"] == 1).sum()),
        "precision_of_alerts": float((alerts["is_fraud"] == 1).mean()),
        "fraud_dollars_in_alerts": fraud_dollars,
        "window_days": window_days,
    }
    print(f"      {len(alerts):,} alerts over {window_days:.1f} days, "
          f"{int((alerts['is_fraud'] == 1).sum()):,} fraud "
          f"(${fraud_dollars:,.0f})")

    cap = cfg.capacity
    demand_hours = len(alerts) * (cap.minutes_per_review / 60.0)
    supply_hours = cap.reviews_per_day * (cap.minutes_per_review / 60.0) * window_days
    results["capacity"] = {
        "n_analysts": cap.n_analysts,
        "shift_hours_per_day": cap.shift_hours_per_day,
        "minutes_per_review": cap.minutes_per_review,
        "reviews_per_day": cap.reviews_per_day,
        "demand_review_hours": demand_hours,
        "supply_review_hours": supply_hours,
        "supply_over_demand_pct": 100.0 * supply_hours / demand_hours,
    }
    print(f"      capacity {supply_hours:,.0f}h vs demand {demand_hours:,.0f}h "
          f"({100.0 * supply_hours / demand_hours:.0f}%)")

    # ------------------------------------------------------------- policies
    print("[2/5] Simulating policies at full capacity ...")
    runs = {name: simulate(alerts, p, cap, cfg.sla) for name, p in POLICIES.items()}
    comparison = metrics.compare_policies(runs, cfg.cost)
    comparison.to_csv(config.TABLES / "policy_comparison.csv", index=False)
    results["policy_comparison"] = comparison.to_dict("records")

    for _, r in comparison.iterrows():
        print(f"      {r['policy']:<20} reviewed {r['n_reviewed']:>6,}  "
              f"fraud$ {r['fraud_dollars_caught']:>10,.0f} "
              f"({r['fraud_dollars_caught_pct']:.1f}%)  "
              f"medTTD {r['median_hours_to_detection']:.1f}h  maxWait {r['max_wait_any_alert']:.0f}h  "
              f"SLA breach {r['sla_breach_pct']:.1f}%")

    ttd = {n: metrics.time_to_detection_distribution(r) for n, r in runs.items()}
    for n, d in ttd.items():
        d.to_csv(config.TABLES / f"time_to_detection_{n}.csv", index=False)
    results["time_to_detection"] = {n: d.to_dict("records") for n, d in ttd.items()}

    # ------------------------------------------------------------- capacity
    print("[3/5] Capacity sweep ...")
    sweep = metrics.capacity_sweep(alerts, POLICIES, cap, cfg.sla, cost=cfg.cost)
    sweep.to_csv(config.TABLES / "capacity_sweep.csv", index=False)
    results["capacity_sweep"] = sweep.to_dict("records")

    headroom = {n: metrics.capacity_headroom(sweep, n) for n in POLICIES}
    results["capacity_headroom"] = headroom
    for n, h in headroom.items():
        if h.get("min_capacity_scale") == h.get("min_capacity_scale"):  # not NaN
            print(f"      {n:<20} can cut own capacity {h['capacity_reduction_pct']:.0f}% "
                  f"({h['review_hours_saved']:.0f}h saved) for <=5pp capture loss")

    # Equivalence is reported across the whole staffing range, not just at full
    # capacity. At full capacity the queue nearly clears under any policy, so
    # the gap there understates prioritisation badly; the gains appear precisely
    # where the operation is short-staffed, which is when it matters.
    equiv_rows = []
    for scale in (0.3, 0.5, 0.7, 0.85, 1.0):
        for c in ("risk_ranked", "risk_ranked_aging"):
            e = metrics.policy_capacity_equivalence(sweep, "fifo", c, baseline_scale=scale)
            if e.get("challenger_can_match"):
                equiv_rows.append(e)
    equiv_df = pd.DataFrame(equiv_rows)
    equiv_df.to_csv(config.TABLES / "capacity_equivalence.csv", index=False)
    results["capacity_equivalence_vs_fifo"] = equiv_rows
    for e in equiv_rows:
        if e["challenger"] != "risk_ranked":
            continue
        print(f"      risk_ranked matches FIFO@{e['baseline_capacity_scale']:.0%} "
              f"({e['target_value']:.1f}% capture) on {e['challenger_review_hours']:.0f}h "
              f"vs {e['baseline_review_hours']:.0f}h "
              f"({e['capacity_reduction_pct']:.0f}% less, "
              f"{e['review_hours_saved']:.0f}h saved)")

    # ------------------------------------------------- auto-disposition
    print("[4/5] Auto-disposition sweep ...")
    curve = auto.auto_disposition_curve(alerts, cap, cfg.sla, cost=cfg.cost)
    curve.to_csv(config.TABLES / "auto_disposition_curve.csv", index=False)
    results["auto_disposition_curve"] = curve.to_dict("records")
    band = auto.best_auto_close_band(curve, max_dollar_loss_pct=2.0)
    results["auto_disposition_band"] = band
    if band.get("feasible"):
        print(f"      auto-close below {band['auto_close_below']:.4f} closes "
              f"{band['auto_closed_pct']:.1f}% of alerts, saves "
              f"{band['review_hours_saved']:.0f}h, capture "
              f"{band['fraud_dollars_caught_pct']:.1f}% "
              f"(vs {band['baseline_fraud_dollars_caught_pct']:.1f}%)")

    # ------------------------------------------------------------- figures
    print("[5/5] Figures ...")
    plots.plot_capacity_curve(sweep, config.FIGURES / "capacity_curve.png")
    plots.plot_queue_depth({n: r.queue_trace for n, r in runs.items()},
                           config.FIGURES / "queue_depth.png")
    plots.plot_time_to_detection(
        {n: r.alerts.loc[r.alerts["fraud_caught"], "wait_hours"].dropna()
         for n, r in runs.items()},
        config.FIGURES / "time_to_detection.png",
    )
    plots.plot_auto_disposition(curve, config.FIGURES / "auto_disposition.png",
                                chosen=band.get("auto_closed_pct"))
    plots.plot_sla_breach(comparison, config.FIGURES / "sla_breach.png")

    # Queue snapshot for the reviewer interface.
    best_run = runs["risk_ranked_aging"]
    best_run.alerts.to_parquet(config.DATA / "simulated_queue.parquet", index=False)

    results["runtime_seconds"] = round(time.time() - t0, 1)
    (config.TABLES / "run_results.json").write_text(
        json.dumps(results, indent=2, default=_jsonable)
    )
    print(f"\nDone in {results['runtime_seconds']}s -> artifacts/tables/run_results.json")


if __name__ == "__main__":
    main()
