"""Helpers behind the Streamlit reviewer interface.

They live here rather than in ``app/reviewer.py`` so they can be imported and
tested without Streamlit, which is an optional dependency and not installed in
CI. The app imports them; nothing in here imports the app.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .config import DATA

DISPOSITION_LOG = DATA / "disposition_log.csv"

# The alert stream carries up to five TreeSHAP contributors per alert, exported
# by fraud-threshold-lab as signal_{i}_name / _value / _contribution.
MAX_SIGNALS = 5


def signal_frame(row: pd.Series) -> pd.DataFrame:
    """The contributing signals for one alert, in the order the stream carries
    them (strongest first, as exported). Slots without a signal are skipped."""
    rows = []
    for i in range(1, MAX_SIGNALS + 1):
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


def log_disposition(
    alert_id, decision: str, note: str, path: Path = DISPOSITION_LOG
) -> None:
    """Append one reviewer decision to the CSV log, writing the header on the
    first entry. Timestamps are UTC and carry their offset."""
    entry = pd.DataFrame(
        [{
            "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "transaction_id": alert_id,
            "decision": decision,
            "note": note,
        }]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    entry.to_csv(path, mode="a", header=not path.exists(), index=False)
