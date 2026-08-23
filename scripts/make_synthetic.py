"""Generate a synthetic alert stream with the same schema as P1's export."""

from __future__ import annotations

import json

from triage.config import DATA
from triage.data import METADATA_FILE, STREAM_FILE, generate_synthetic_stream


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    stream = generate_synthetic_stream()
    stream.to_parquet(DATA / STREAM_FILE, index=False)
    (DATA / METADATA_FILE).write_text(
        json.dumps(
            {
                "source_project": "SYNTHETIC — not real model output",
                "operating_threshold": 0.05,
                "n_rows": int(len(stream)),
                "n_fraud": int(stream["is_fraud"].sum()),
            },
            indent=2,
        )
    )
    print(f"Synthetic stream written to {DATA / STREAM_FILE} ({len(stream):,} rows)")
    print("NOTE: numbers from a synthetic run are not reportable.")


if __name__ == "__main__":
    main()
