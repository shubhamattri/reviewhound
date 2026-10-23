# Reviewhound

A pull-request reviewer that developers can trust. The model proposes findings; code checks
every one of them before anything is posted. It measures its own precision on a replay set.

## How it works

```
GitHub webhook ──> webhook.py ──> SQLite job queue (one job per repo + PR + head commit)
                                        │
                                   worker.py (lease, retry with backoff, dead-letter)
                                        │
                                   pipeline.py
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ fetch PR + diff ─> config from BASE branch (.reviewhound.yml, defaults)        │
   │ risk router ─> tier (skip / light / standard / deep), model, byte budget       │
   │ tree-sitter index of head (Python, TypeScript) + learned conventions           │
   │ context: diff with head line numbers, changed functions, callers, referenced   │
   │          types, recent history, all inside the byte budget                     │
   │ model registry (claude -p, fallback, circuit breaker) ─> JSON findings         │
   │ claim checker: quote exists, evidence is on changed lines, symbols exist,      │
   │                calls / not_calls hold. One false claim drops the finding.      │
   │ rule scanners (built-in + Semgrep if installed): secrets, SQL joins, PII logs  │
   │ policy: severity floor, verified only, one comment per line, max comments,     │
   │         never repeat a posted or declined finding, verdict                     │
   │ post (only if config says posting: true and the head did not move) or store    │
   └──────────────────────────────────────────────────────────────────────────────┘
                                        │
                     store.py: reviews, findings with stable fingerprints, feedback
```

Design rules: the engine has no team words in it (everything team-specific is in
`.reviewhound.yml`); a new repo runs in shadow mode (stores findings, posts nothing) until its
config says `posting: true`; config is read from the base branch so a PR cannot change how
it is reviewed.

## Run it

```bash
uv sync
uv run pytest -q                      # 89 tests, no network
uv run reviewhound eval               # replay recorded answers: precision / recall
uv run reviewhound eval --no-checker  # same, with the claim checker switched off
uv run reviewhound eval --live        # ask the real model (uses `claude -p`, your subscription)

# Review a real PR and print the exact review it would post. Token from GITHUB_TOKEN or gh.
uv run reviewhound --gh-user <account> review owner/repo 12 --dry-run

# Service mode
REVIEWHOUND_WEBHOOK_SECRET=... uv run reviewhound serve --port 8787
uv run reviewhound worker
uv run reviewhound enqueue owner/repo 12     # manual / missed-PR sweep
uv run reviewhound feedback owner/repo 12 <fingerprint> declined --detail "intended"
```

Model calls go through `claude -p` with `ANTHROPIC_API_KEY` removed from the environment, in an
empty working directory with only local settings loaded, so no other project's instructions
leak into a review.

## Config (`.reviewhound.yml`, all optional)

```yaml
posting: false            # shadow mode until true
languages: [python, typescript]
ignore_paths: ["**/*.lock", "**/dist/**"]
risk_paths:
  - {pattern: "**/billing/**", tier: deep}
  - {pattern: "docs/**", tier: light}
auth:
  guard_functions: [require_role]
personal_data_terms: [email, phone, dob]
tone_examples: ["Short and direct. Say what breaks and for whom."]
policy:
  min_severity_to_post: minor
  max_comments: 10
  blocking_severity: blocker
  drop_unverified: true
budget:
  context_bytes_standard: 60000
```

Unknown keys or bad values fail the review loudly instead of being guessed.

## Evals

`evals/build_cases.py` writes `evals/cases/*`: 12 small PRs (Python and TypeScript) with seeded
bugs, two clean PRs, expected findings, and a recorded model answer that includes deliberate
hallucinations (invented helpers, quotes that do not exist, bugs in unchanged code, false
"missing guard" claims). Any prompt or checker change must not lower precision on this set.

## Not in milestone 1

- Proof by generated failing test in a sandbox (milestone 2).
- Feedback from GitHub events (thread resolved, replies); today `reviewhound feedback` records it.
- GitHub App installation tokens; today a user token (`GITHUB_TOKEN` or `gh`) is used.
- Comments on deleted lines; findings must sit on lines the PR added.
- A post that times out is recorded as `post-uncertain` and not retried, so it may be missing
  or present on GitHub; nothing posts it twice automatically.
