# Contributing to openrot

**English** · [Русский](CONTRIBUTING.ru.md)

Thanks for contributing! This project is small and friendly. Issues, questions,
and pull requests are all welcome. Below is how things work so your change lands
cleanly.

## Project layout

- `openrot/` — the package itself (`openrot/cli.py` is the Typer entry point).
- `tests/` — `pytest` suite covering the package.
- `Makefile` — the canonical dev commands (`make check`, `make fix`, ...).
- `pyproject.toml` — dependencies and tool config (ruff, mypy, pytest).

## Setup

Requires Python 3.14+ and [Poetry](https://python-poetry.org).

```bash
poetry install            # installs deps + dev group
pre-commit install        # optional: run ruff/mypy on every commit
```

## Development loop

```bash
make lint     # ruff check + ruff format --check (no file changes)
make types    # mypy strict on openrot/
make test     # pytest
make check    # lint + types + tests, everything at once
```

To auto-format and auto-fix before committing:

```bash
make fix      # ruff format + ruff check --fix (mutates files)
```

## Before you open a PR

1. Make sure `make check` passes locally. CI runs the same
   [checks](.github/workflows/ci.yml) (ruff, mypy, pytest) on every push.
2. Run `make fix` so formatting is consistent.
3. Keep changes focused and add tests for new behavior. Bug fixes should include
   a regression test; new commands/flags should have coverage too.
4. Write a clear commit message describing what changed and why.

## Code style

- Python 3.14+, [ruff](https://docs.astral.sh/ruff/) with the repo config
  (`line-length = 88`, double quotes), and strict [mypy](https://mypy-lang.org).
- Match the surrounding style: readable, small functions, docstrings on public
  symbols (D-style rules are enforced by ruff).
- No code comments unless they clarify something non-obvious.

## Tests

The suite lives in `tests/` and uses plain `pytest`. Run a single file or test
with:

```bash
poetry run pytest tests/test_foo.py
```

## Reporting bugs

Open an issue with the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md)
and include:

- your command(s) and their output (e.g. `openrot doctor`),
- your `config.yaml` (redact anything sensitive),
- OS + whether you are on the host or in Docker,
- the sing-box and, if relevant, warp-cli versions.

## License

By contributing you agree that your work is licensed under the
[MIT License](LICENSE), same as the rest of the project.
