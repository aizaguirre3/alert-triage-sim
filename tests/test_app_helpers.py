"""The reviewer interface's helpers, tested without Streamlit.

``app/reviewer.py`` runs its page at import time and needs Streamlit, which is
optional and absent in CI. The two pieces of logic it relies on live in
``triage.review_helpers`` so they can be exercised here directly.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from triage.review_helpers import log_disposition, signal_frame


def _row(**signals) -> pd.Series:
    """A hand-built alert row with the stream's signal_{i}_* layout."""
    data = {"transaction_id": 42, "score": 0.91, "amount": 250.0}
    for i, (name, value, contrib) in enumerate(signals.get("signals", []), start=1):
        data[f"signal_{i}_name"] = name
        data[f"signal_{i}_value"] = value
        data[f"signal_{i}_contribution"] = contrib
    return pd.Series(data)


# ------------------------------------------------------------- signal_frame


def test_signal_frame_lists_each_signal_with_its_direction():
    row = _row(signals=[("C14", 1.0, 1.72), ("card6_debit", 1.0, -0.41), ("TransactionAmt", 250.0, 0.08)])
    sig = signal_frame(row)

    assert sig["Signal"].tolist() == ["C14", "card6_debit", "TransactionAmt"]
    assert sig["Value"].tolist() == [1.0, 1.0, 250.0]
    assert sig["Contribution"].tolist() == [1.72, -0.41, 0.08]
    assert sig["Direction"].tolist() == ["↑ raises risk", "↓ lowers risk", "↑ raises risk"]


def test_signal_frame_skips_empty_signal_slots():
    """The stream pads to five signals; alerts with fewer carry NaN names."""
    row = _row(signals=[("C14", 1.0, 1.72)])
    for i in range(2, 6):
        row[f"signal_{i}_name"] = np.nan
        row[f"signal_{i}_value"] = np.nan
        row[f"signal_{i}_contribution"] = np.nan
    sig = signal_frame(row)
    assert len(sig) == 1
    assert sig["Signal"].iloc[0] == "C14"


def test_signal_frame_is_empty_when_the_row_has_no_attributions():
    sig = signal_frame(_row())
    assert sig.empty


# --------------------------------------------------------- log_disposition


def test_log_disposition_appends_rows_and_writes_the_header_once(tmp_path):
    log = tmp_path / "nested" / "disposition_log.csv"
    log_disposition(42, "confirmed_fraud", "card-testing pattern", path=log)
    log_disposition(43, "cleared", "", path=log)

    got = pd.read_csv(log, keep_default_na=False)
    assert list(got.columns) == ["logged_at", "transaction_id", "decision", "note"]
    assert got["transaction_id"].tolist() == [42, 43]
    assert got["decision"].tolist() == ["confirmed_fraud", "cleared"]
    assert got["note"].tolist() == ["card-testing pattern", ""]


def test_log_disposition_timestamps_are_timezone_aware_utc(tmp_path):
    log = tmp_path / "disposition_log.csv"
    log_disposition(1, "escalated", "", path=log)
    stamp = pd.read_csv(log)["logged_at"].iloc[0]
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
