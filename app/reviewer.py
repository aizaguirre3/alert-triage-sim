"""Streamlit reviewer interface.

The requirement this is built against: a reviewer should understand *why* an
alert scored high without leaving the screen. So the per-alert view leads with
the contributing signals and their direction, not with the score alone. A bare
number tells an analyst nothing they can act on or push back against.

Run with:  streamlit run app/reviewer.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from triage.config import DATA, SECONDS_PER_HOUR, DEFAULT  # noqa: E402
from triage.data import generate_synthetic_stream, load_alert_stream, select_alerts  # noqa: E402
from triage.simulator import Policy, simulate  # noqa: E402

QUEUE_FILE = DATA / "simulated_queue.parquet"
DISPOSITION_LOG = DATA / "disposition_log.csv"

st.set_page_config(page_title="Fraud Alert Triage", layout="wide")


@st.cache_data
def load_queue() -> tuple[pd.DataFrame, str]:
    """Prefer the simulated queue; fall back to the stream, then to synthetic."""
    if QUEUE_FILE.exists():
        return pd.read_parquet(QUEUE_FILE), "simulated queue (risk-ranked + aging)"
    try:
        stream, meta = load_alert_stream()
        alerts = select_alerts(stream, float(meta.get("operating_threshold", 0.05)))
        res = simulate(alerts, Policy.RISK_RANKED_AGING,
                       DEFAULT.capacity, DEFAULT.sla)
        return res.alerts, "alert stream, simulated on the fly"
    except FileNotFoundError:
        stream = generate_synthetic_stream(n=20_000)
        alerts = select_alerts(stream, 0.05)
        res = simulate(alerts, Policy.RISK_RANKED_AGING,
                       DEFAULT.capacity, DEFAULT.sla)
        return res.alerts, "SYNTHETIC demo data — not real model output"


def signal_frame(row: pd.Series) -> pd.DataFrame:
    """The contributing signals for one alert, strongest first."""
    rows = []
    for i in range(1, 6):
        name = row.get(f"signal_{i}_name")
        if name is None or pd.isna(name):
            continue
        contrib = row.get(f"signal_{i}_contribution", float("nan"))
        rows.append(
            {
                "Signal": name,
                "Value": row.get(f"signal_{i}_value"),
                "Contribution": contrib,
                "Direction": "↑ raises risk" if contrib > 0 else "↓ lowers risk",
            }
        )
    return pd.DataFrame(rows)


def log_disposition(alert_id, decision: str, note: str) -> None:
    entry = pd.DataFrame(
        [{
            "logged_at": datetime.utcnow().isoformat(timespec="seconds"),
            "transaction_id": alert_id,
            "decision": decision,
            "note": note,
        }]
    )
    DISPOSITION_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry.to_csv(
        DISPOSITION_LOG, mode="a", header=not DISPOSITION_LOG.exists(), index=False
    )


queue, source = load_queue()

st.title("Fraud Alert Review Queue")
if source.startswith("SYNTHETIC"):
    st.warning(f"Data source: {source}")
else:
    st.caption(f"Data source: {source}")

# ------------------------------------------------------------------ sidebar
st.sidebar.header("Queue filters")
sla_hours = st.sidebar.slider("SLA deadline (hours)", 4, 72,
                              int(DEFAULT.sla.deadline_hours), 4)
min_score = st.sidebar.slider("Minimum risk score", 0.0, 1.0, 0.0, 0.01)
min_amount = st.sidebar.number_input("Minimum amount ($)", value=0.0, step=25.0)
show_only_pending = st.sidebar.checkbox("Pending review only", value=True)
top_n = st.sidebar.slider("Alerts to show", 10, 200, 50, 10)

view = queue.copy()
if "score" not in view.columns and "score_raw" in view.columns:
    view["score"] = view["score_raw"]
view = view[(view["score"] >= min_score) & (view["amount"] >= min_amount)]
if show_only_pending and "reviewed" in view.columns:
    view = view[~view["reviewed"]]

# Queue age drives priority, so it is computed for display rather than assumed.
now_dt = float(queue["arrival_dt"].max())
view["age_hours"] = (now_dt - view["arrival_dt"]) / SECONDS_PER_HOUR
view["sla_remaining_hours"] = sla_hours - view["age_hours"]
view["breaching"] = view["sla_remaining_hours"] <= 0

view = view.sort_values(["breaching", "score"], ascending=[False, False]).head(top_n)

# ------------------------------------------------------------------ summary
c1, c2, c3, c4 = st.columns(4)
c1.metric("Alerts in view", f"{len(view):,}")
c2.metric("Breaching SLA", f"{int(view['breaching'].sum()):,}")
c3.metric("Value at risk", f"${view['amount'].sum():,.0f}")
c4.metric("Median risk score", f"{view['score'].median():.3f}" if len(view) else "—")

st.divider()
left, right = st.columns([1.15, 1])

with left:
    st.subheader("Prioritized queue")
    st.caption(
        "Ordered by SLA breach first, then risk score — the order the "
        "risk-ranked-with-aging policy would work them in."
    )
    cols = ["transaction_id", "score", "amount", "age_hours",
            "sla_remaining_hours", "breaching"]
    cols = [c for c in cols if c in view.columns]
    st.dataframe(
        view[cols].style.format({
            "score": "{:.4f}",
            "amount": "${:,.2f}",
            "age_hours": "{:.1f}h",
            "sla_remaining_hours": "{:.1f}h",
        }),
        use_container_width=True,
        height=430,
    )

with right:
    st.subheader("Alert detail")
    if len(view) == 0:
        st.info("No alerts match the current filters.")
    else:
        ids = view["transaction_id"].tolist()
        chosen = st.selectbox("Transaction", ids, index=0)
        row = view[view["transaction_id"] == chosen].iloc[0]

        m1, m2, m3 = st.columns(3)
        m1.metric("Risk score", f"{row['score']:.4f}")
        m2.metric("Amount", f"${row['amount']:,.2f}")
        m3.metric("Queue age", f"{row['age_hours']:.1f}h")

        if row["breaching"]:
            st.error(f"SLA breached by {abs(row['sla_remaining_hours']):.1f} hours")
        else:
            st.success(f"{row['sla_remaining_hours']:.1f} hours until SLA deadline")

        st.markdown("**Why this alert scored high**")
        sig = signal_frame(row)
        if sig.empty:
            st.caption("No attribution data available for this alert.")
        else:
            st.dataframe(
                sig.style.format({"Contribution": "{:+.4f}"}).bar(
                    subset=["Contribution"], align="zero", color=["#d98880", "#7dcea0"]
                ),
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                "TreeSHAP contributions in log-odds. Positive values pushed this "
                "transaction toward fraud relative to the model's base rate."
            )

        ctx = {k: row[k] for k in
               ("ProductCD", "card4", "card6", "DeviceType", "identity_present")
               if k in row.index and pd.notna(row[k])}
        if ctx:
            st.markdown("**Transaction context**")
            st.table(pd.DataFrame([ctx]))

        st.markdown("**Disposition**")
        note = st.text_input("Reviewer note", key=f"note_{chosen}")
        b1, b2, b3 = st.columns(3)
        if b1.button("Confirm fraud", type="primary", use_container_width=True):
            log_disposition(chosen, "confirmed_fraud", note)
            st.success("Logged as confirmed fraud.")
        if b2.button("Clear as legitimate", use_container_width=True):
            log_disposition(chosen, "cleared", note)
            st.success("Logged as cleared.")
        if b3.button("Escalate", use_container_width=True):
            log_disposition(chosen, "escalated", note)
            st.warning("Escalated for further investigation.")

if DISPOSITION_LOG.exists():
    with st.expander("Disposition log"):
        st.dataframe(pd.read_csv(DISPOSITION_LOG).tail(50),
                     use_container_width=True, hide_index=True)
