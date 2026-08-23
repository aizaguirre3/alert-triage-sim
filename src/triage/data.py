"""Loading the alert stream, and a synthetic generator for tests and CI.

The real stream comes from ``fraud-threshold-lab``'s exporter: scored
transactions with timestamps, amounts, outcomes, and TreeSHAP attributions.
The synthetic generator produces a stream with the same schema so the test
suite and CI can run without the Kaggle data, which is not redistributable.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DATA, SECONDS_PER_DAY

STREAM_FILE = "alert_stream.parquet"
METADATA_FILE = "alert_stream_metadata.json"

REQUIRED_COLUMNS = ("transaction_id", "transaction_dt", "amount", "is_fraud")


def load_alert_stream(
    path: Path | None = None, score_col: str = "score_raw"
) -> tuple[pd.DataFrame, dict]:
    """Read the exported stream and normalise it to the simulator's schema."""
    d = Path(path) if path is not None else DATA
    stream_path = d / STREAM_FILE if d.is_dir() else d
    if not stream_path.exists():
        raise FileNotFoundError(
            f"No alert stream at {stream_path}. Run `make data`, which copies it "
            "from a local fraud-threshold-lab checkout, or `make synthetic` to "
            "generate a stand-in stream."
        )
    df = pd.read_parquet(stream_path)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Alert stream is missing column(s): {missing}")
    if score_col not in df.columns:
        raise ValueError(
            f"Score column {score_col!r} not in stream. Available: "
            f"{[c for c in df.columns if c.startswith('score')]}"
        )

    out = df.copy()
    out["arrival_dt"] = out["transaction_dt"].astype(float)
    out["score"] = out[score_col].astype(float)

    meta_path = stream_path.parent / METADATA_FILE
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return out.sort_values("arrival_dt").reset_index(drop=True), meta


def select_alerts(
    stream: pd.DataFrame, threshold: float, score_col: str = "score"
) -> pd.DataFrame:
    """Everything at or above the detection threshold becomes an alert."""
    return stream[stream[score_col] >= threshold].reset_index(drop=True)


def generate_synthetic_stream(
    n: int = 40_000,
    fraud_rate: float = 0.035,
    days: float = 30.0,
    seed: int = 20260809,
    start_dt: float = 13_151_846.0,
) -> pd.DataFrame:
    """A stream shaped like the real one, for tests and CI.

    Deliberately imperfect in the same ways: scores separate fraud from
    non-fraud only partially, arrivals follow a daily cycle so the queue sees
    genuine peaks and troughs, and fraud amounts are heavier-tailed than
    legitimate ones.
    """
    rng = np.random.default_rng(seed)
    is_fraud = rng.binomial(1, fraud_rate, n)

    # Daily arrival cycle: a broad afternoon peak rather than uniform arrivals.
    u = rng.uniform(0, 1, n)
    day = rng.uniform(0, days, n)
    hour = 24 * (0.5 + 0.35 * np.sin(2 * np.pi * (u - 0.25)))
    arrival = start_dt + day * SECONDS_PER_DAY + hour * 3600

    # Scores: overlapping beta distributions, fraud shifted up but not separated.
    score = np.where(
        is_fraud == 1,
        rng.beta(2.2, 5.0, n),
        rng.beta(1.2, 22.0, n),
    )
    score = np.clip(score, 1e-6, 1 - 1e-6)

    amount = np.where(
        is_fraud == 1,
        rng.lognormal(4.6, 1.25, n),
        rng.lognormal(4.0, 1.05, n),
    ).round(2)

    df = pd.DataFrame(
        {
            "transaction_id": np.arange(1, n + 1),
            "transaction_dt": arrival,
            "arrival_dt": arrival,
            "amount": amount,
            "is_fraud": is_fraud,
            "score_raw": score,
            "score": score,
            "ProductCD": rng.choice(["W", "C", "R", "H", "S"], n,
                                    p=[0.74, 0.12, 0.06, 0.05, 0.03]),
            "card4": rng.choice(["visa", "mastercard", "amex", "discover"], n,
                                p=[0.65, 0.30, 0.03, 0.02]),
            "card6": rng.choice(["debit", "credit"], n, p=[0.74, 0.26]),
            "DeviceType": rng.choice(["mobile", "desktop", None], n, p=[0.4, 0.35, 0.25]),
            "identity_present": rng.binomial(1, 0.24, n),
        }
    )
    for i in range(1, 6):
        df[f"signal_{i}_name"] = rng.choice(
            ["C13", "V257", "card1", "TransactionAmt", "D15", "id_02",
             "grp_card1__amt_ratio", "P_emaildomain"], n
        )
        df[f"signal_{i}_value"] = rng.normal(0, 1, n).round(3)
        df[f"signal_{i}_contribution"] = (
            rng.normal(0, 1, n) * (1.0 / i) * np.where(is_fraud == 1, 1.4, 1.0)
        ).round(4)
    df["shap_base_value"] = -3.3

    return df.sort_values("arrival_dt").reset_index(drop=True)
