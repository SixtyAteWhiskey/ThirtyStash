# Contributing to ThirtyStash

Thanks for helping improve ThirtyStash.

## Development setup

Python 3.12 is the reference development version.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python scripts/fetch_vendor.py
python app.py
```

ThirtyStash will listen on `http://127.0.0.1:3055` when run directly.

## Before submitting a pull request

```bash
python -m compileall -q app.py backup_scheduler.py scripts tests
pytest -q
```

If Docker is available, also verify:

```bash
docker compose config
docker compose build
```

Please keep changes focused. ThirtyStash intentionally favors a small,
reliable single-household architecture over feature complexity.
