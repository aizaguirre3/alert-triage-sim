PY := PYTHONPATH=src .venv/bin/python
UV := uv

.PHONY: all setup data synthetic run app test clean

## Full reproduction: environment, stream, simulation, tests.
all: setup data run test

setup:
	$(UV) venv --python 3.12
	$(UV) pip install -e ".[dev,app]"

## Copy the exported alert stream from a local fraud-threshold-lab checkout.
data:
	$(PY) scripts/fetch_stream.py

## Generate a stand-in stream if fraud-threshold-lab is unavailable.
synthetic:
	$(PY) scripts/make_synthetic.py

run:
	$(PY) scripts/run_simulation.py

app:
	PYTHONPATH=src .venv/bin/streamlit run app/reviewer.py

test:
	.venv/bin/python -m pytest

clean:
	rm -rf artifacts/figures/* artifacts/tables/*
