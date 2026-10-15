# Contributing

Python 3.14 is the development interpreter and 3.9 the supported floor; a
change is done when the suite passes on both. One venv per interpreter:

```bash
python3.14 -m venv .venv/3.14
.venv/3.14/bin/python -m pip install -e ".[dev,docs]"   # Scripts\python on Windows
.venv/3.14/bin/python -m pytest -q
```

The `dev` extra pulls every transport, so `".[dev]"` is the whole list.

Before committing:

```bash
python -m black src tests
python -m mkdocs build --strict   # when docs changed
```

Layout, environments, CI, releases and the conventions the source does not
make obvious are in [AGENTS.md](AGENTS.md).
