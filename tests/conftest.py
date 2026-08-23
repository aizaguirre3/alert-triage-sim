"""Fixtures. Synthetic only -- the Kaggle-derived stream is not redistributable,
and none of the queue logic needs it."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from triage.config import CapacityConfig, CostConfig, SLAConfig
from triage.data import generate_synthetic_stream

HOUR = 3600.0
DAY = 86400.0


@pytest.fixture
def cost() -> CostConfig:
    return CostConfig()


@pytest.fixture
def sla() -> SLAConfig:
    return SLAConfig()


@pytest.fixture
def tiny_capacity() -> CapacityConfig:
    """One reviewer, one review per hour, an 8-hour shift -- 8 reviews per day.

    Small enough that the exact review sequence can be worked out by hand.
    """
    return CapacityConfig(
        n_analysts=1,
        minutes_per_review=60.0,
        shift_start_hour=8.0,
        shift_hours_per_day=8.0,
    )


@pytest.fixture
def tiny_alerts() -> pd.DataFrame:
    """Six alerts arriving before the shift opens on day 1.

    Because they all arrive before any review slot, the whole queue is visible
    at the first slot and the policies differ purely in ordering -- which is
    exactly what the ordering tests need to isolate.
    """
    base = 10 * DAY  # day 10, midnight
    return pd.DataFrame(
        {
            "transaction_id": [1, 2, 3, 4, 5, 6],
            "arrival_dt": [base + i * 60 for i in range(6)],
            "score": [0.10, 0.90, 0.30, 0.70, 0.50, 0.99],
            "amount": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0],
            "is_fraud": [0, 1, 0, 1, 0, 1],
        }
    )


@pytest.fixture
def synthetic_alerts() -> pd.DataFrame:
    from triage.data import select_alerts

    return select_alerts(generate_synthetic_stream(n=8000, seed=7), 0.05)
