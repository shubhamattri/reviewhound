# Contributing

Thanks for helping. Reviewhound only works if developers trust it, so every change is held
to one rule: it must not lower precision on the eval set.

## Setup

```bash
uv sync
uv run pytest -q
uv run mypy && uv run ruff check src tests evals qa
uv run reviewhound eval
```

The suite never touches the network. Model calls in tests go through `FakeProvider`.

## What a good pull request looks like

- One change, with a test that fails before it and passes after.
- If you add or change a guard (a check that drops, holds or blocks something), break it on
  purpose and show the test that catches it.
- Prompt, checker or scanner changes: paste `reviewhound eval` output before and after.
  Precision must not go down. Add an eval case in `evals/build_cases.py` when you fix a miss.
- No company-specific words in the engine. Team rules belong in `.reviewhound.yml`.

## Adding an eval case

Add an entry to `CASES` in `evals/build_cases.py` (base files, head files, expected findings,
and a recorded model answer, using anchor text instead of line numbers), then run
`uv run python evals/build_cases.py`.
