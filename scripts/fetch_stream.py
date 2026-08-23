"""Copy the exported alert stream from a local fraud-threshold-lab checkout.

The stream is derived from IEEE-CIS data, which is not redistributable, so it is
not committed here. Run P1 once, then run this.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from triage.config import DATA
from triage.data import METADATA_FILE, STREAM_FILE

DEFAULT_SOURCES = (
    Path.home() / "Projects" / "fraud-threshold-lab" / "data" / "processed",
    Path(__file__).resolve().parents[2] / "fraud-threshold-lab" / "data" / "processed",
)


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    candidates = [src] if src else list(DEFAULT_SOURCES)

    for cand in candidates:
        if cand and (cand / STREAM_FILE).exists():
            DATA.mkdir(parents=True, exist_ok=True)
            for name in (STREAM_FILE, METADATA_FILE):
                if (cand / name).exists():
                    shutil.copy2(cand / name, DATA / name)
                    print(f"  {name} <- {cand / name}")
            print("Alert stream ready in", DATA)
            return

    sys.exit(
        "Could not find an exported alert stream. Looked in:\n  "
        + "\n  ".join(str(c) for c in candidates)
        + "\n\nRun `make all` in fraud-threshold-lab first, or pass the path:\n"
        "  python scripts/fetch_stream.py /path/to/fraud-threshold-lab/data/processed\n"
        "Alternatively run `make synthetic` to generate a stand-in stream."
    )


if __name__ == "__main__":
    main()
